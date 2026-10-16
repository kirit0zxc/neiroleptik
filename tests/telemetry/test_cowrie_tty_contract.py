"""TDD реального wire-формата Cowrie; bytes собраны независимо от будущего parser."""

import base64
import importlib
import io
import json
import struct
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
SECOND = 1_760_518_800


def frame(op, payload=b"", *, direction=0, sec=SECOND, usec=0, tty=0, length=None):
    return (
        struct.pack(
            "<iLiiLL", op, tty, len(payload) if length is None else length, direction, sec, usec
        )
        + payload
    )


def chunk(seq, offset_ms, direction, payload):
    return dict(
        seq=seq,
        offset_ms=offset_ms,
        direction=direction,
        data_base64=base64.b64encode(payload).decode("ascii"),
        truncated=False,
    )


class ShortReads(io.BytesIO):
    """Однобайтовый поток обнаруживает предположение о полном read(n)."""

    def read(self, size=-1):
        assert 0 <= size <= 4096
        return super().read(min(size, 1))


@pytest.fixture
def api():
    try:
        return importlib.import_module("agent.telemetry.cowrie_tty")
    except ModuleNotFoundError as exc:
        if exc.name != "agent.telemetry.cowrie_tty":
            raise
        pytest.fail("TDD: требуется реализация agent.telemetry.cowrie_tty", pytrace=False)


def convert(api, data, *, sealed=True, limits=None):
    stream = ShortReads(data)
    result = api.CowrieTtyConverter().convert(stream, sealed=sealed, limits=limits)
    assert not stream.closed
    return result


@pytest.mark.parametrize("payload", [b"", b"hello\r\n", b"\xff\x00\x1b]8;;SECRET\x07"])
def test_expected_chunks_fit_existing_schema(payload):
    schema = json.loads((ROOT / "contracts/replay-chunk-v1.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema).validate(chunk(0, 0, "out", payload))


def test_exact_timing_binary_directions_and_split_utf8(api):
    encoded = "я".encode()
    data = frame(1, usec=999000)
    data += frame(3, b"whoami\n", direction=1, usec=999999)
    data += frame(3, encoded[:1], direction=2, sec=SECOND + 1, usec=1000)
    data += frame(3, encoded[1:] + b"\xff\x1b[31m<script>", direction=2, sec=SECOND + 1, usec=1000)
    data += frame(2, sec=SECOND + 1, usec=2000)
    result = convert(api, data)
    assert result.complete and result.code is None
    assert result.started_at == datetime.fromtimestamp(SECOND, UTC).replace(microsecond=999000)
    assert result.consumed_bytes == len(data)
    assert result.chunks == (
        chunk(0, 0, "in", b"whoami\n"),
        chunk(1, 2, "out", encoded[:1]),
        chunk(2, 2, "out", encoded[1:] + b"\xff\x1b[31m<script>"),
    )
    assert convert(api, data).chunks == result.chunks


def test_empty_sealed_recording_and_empty_write(api):
    empty = convert(api, frame(1) + frame(2))
    assert empty.complete and empty.chunks == ()
    result = convert(api, frame(1) + frame(3, direction=1) + frame(2))
    assert result.chunks == (chunk(0, 0, "in", b""),)


@pytest.mark.parametrize(
    "sealed,tail,code",
    [
        (False, b"", "RECORDING_ACTIVE"),
        (False, frame(2), "AWAITING_SEAL"),
        (True, b"", "MISSING_CLOSE"),
    ],
)
def test_eof_and_close_do_not_replace_seal(api, sealed, tail, code):
    result = convert(api, frame(1) + tail, sealed=sealed)
    assert not result.complete and result.code == code


@pytest.mark.parametrize(
    "tail,code",
    [
        (b"x", "INCOMPLETE_HEADER"),
        (frame(3, b"ab", direction=2, length=3), "INCOMPLETE_PAYLOAD"),
        (frame(4), "UNKNOWN_OPERATION"),
        (frame(99), "UNKNOWN_OPERATION"),
        (frame(3, b"x", direction=3), "UNKNOWN_DIRECTION"),
        (frame(3, b"x", direction=0), "UNKNOWN_DIRECTION"),
        (frame(3, direction=2, length=-1), "INVALID_HEADER"),
        (frame(3, direction=2, tty=1), "INVALID_HEADER"),
        (frame(3, direction=2, usec=1000000), "INVALID_TIME"),
        (frame(3, direction=2, sec=SECOND - 1), "REVERSED_TIME"),
        (frame(1), "INVALID_SEQUENCE"),
        (frame(2, b"x"), "INVALID_HEADER"),
        (frame(2, direction=1), "INVALID_HEADER"),
        (frame(2) + frame(3, b"x", direction=2), "INVALID_SEQUENCE"),
    ],
)
def test_errors_keep_only_complete_prefix(api, tail, code):
    prefix = frame(1) + frame(3, b"good", direction=2)
    result = convert(api, prefix + tail)
    assert not result.complete and result.code == code
    assert result.chunks == (chunk(0, 0, "out", b"good"),)
    expected_consumed = len(prefix) + (24 if tail.startswith(frame(2)) else 0)
    assert result.consumed_bytes == expected_consumed


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", "EMPTY_INPUT"),
        (b"x", "INCOMPLETE_HEADER"),
        (frame(3, b"x", direction=2), "INVALID_SEQUENCE"),
        (frame(1, b"x"), "INVALID_HEADER"),
    ],
)
def test_invalid_start(api, data, code):
    result = convert(api, data)
    assert not result.complete and result.code == code
    assert result.chunks == () and result.consumed_bytes == 0
    assert result.started_at is None


def test_partial_payload_can_be_completed_on_next_snapshot(api):
    prefix = frame(1)
    complete = prefix + frame(3, b"abc", direction=2)
    result = convert(api, complete[:-1], sealed=False)
    assert result.code == "INCOMPLETE_PAYLOAD" and result.consumed_bytes == len(prefix)
    result = convert(api, complete + frame(2))
    assert result.complete and result.chunks == (chunk(0, 0, "out", b"abc"),)


def test_budgets_count_headers_and_exact_bytes(api):
    data = frame(1) + frame(3, b"x", direction=2) + frame(2)
    assert convert(api, data, limits=api.CowrieTtyLimits(max_bytes=len(data))).complete
    result = convert(api, data, limits=api.CowrieTtyLimits(max_bytes=len(data) - 1))
    assert result.code == "BYTES_LIMIT" and not result.complete
    assert result.consumed_bytes == 49
    result = convert(api, data, limits=api.CowrieTtyLimits(max_frames=2))
    assert result.code == "FRAME_LIMIT" and not result.complete
    assert result.consumed_bytes == 49


def test_payload_limit_checked_before_read_or_allocation(api):
    result = convert(api, frame(1) + frame(3, direction=2, length=2**31 - 1))
    assert result.code == "PAYLOAD_LIMIT" and result.consumed_bytes == 24
    data = frame(1) + frame(3, b"x" * 65536, direction=2) + frame(2)
    assert convert(api, data).complete


@pytest.mark.parametrize("field", ["max_bytes", "max_frames", "max_payload"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_limits(api, field, value):
    with pytest.raises(ValueError):
        api.CowrieTtyLimits(**{field: value})


@pytest.mark.parametrize("error", [OSError, ValueError, RuntimeError])
def test_external_io_exception_is_redacted(api, error):
    class Broken(io.BytesIO):
        def read(self, size=-1):
            raise error("SECRET-password/path/token")

    result = api.CowrieTtyConverter().convert(Broken(), sealed=True)
    assert result.code == "IO_ERROR" and not result.complete
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("tail", [frame(1), frame(2), frame(3, direction=1)])
def test_any_frame_after_close_is_rejected(api, tail):
    prefix = frame(1) + frame(2)
    result = convert(api, prefix + tail)
    assert result.code == "INVALID_SEQUENCE" and result.consumed_bytes == len(prefix)


@pytest.mark.parametrize("cut", range(1, 24))
def test_partial_headers_never_advance_prefix(api, cut):
    result = convert(api, frame(1) + frame(3, b"x", direction=2)[:cut])
    assert result.code == "INCOMPLETE_HEADER" and result.consumed_bytes == 24


@pytest.mark.parametrize("cut", range(1, 24))
def test_partial_header_at_frame_limit_keeps_incomplete_code(api, cut):
    prefix = frame(1)
    tail = frame(3, b"x", direction=2)[:cut]
    result = convert(api, prefix + tail, limits=api.CowrieTtyLimits(max_frames=1))
    assert result.code == "INCOMPLETE_HEADER"
    assert result.consumed_bytes == len(prefix) and result.chunks == ()


def test_frame_limit_rejects_complete_frame_without_accepting_it(api):
    prefix = frame(1)
    rejected = frame(3, b"bounded", direction=2)
    stream = ShortReads(prefix + rejected + frame(2))
    result = api.CowrieTtyConverter().convert(
        stream, sealed=True, limits=api.CowrieTtyLimits(max_frames=1)
    )
    assert result.code == "FRAME_LIMIT" and result.consumed_bytes == len(prefix)
    assert stream.tell() == len(prefix) + len(rejected)


def test_partial_payload_at_frame_limit_keeps_incomplete_code(api):
    prefix = frame(1)
    tail = frame(3, b"ab", direction=2, length=3)
    result = convert(api, prefix + tail, limits=api.CowrieTtyLimits(max_frames=1))
    assert result.code == "INCOMPLETE_PAYLOAD"
    assert result.consumed_bytes == len(prefix) and result.chunks == ()


@pytest.mark.parametrize("budget", [1, 23, 24, 25, 47])
def test_byte_budget_never_reads_more_than_one_lookahead(api, budget):
    data = frame(1) + frame(3, b"abc", direction=2) + frame(2)
    stream = ShortReads(data)
    result = api.CowrieTtyConverter().convert(
        stream, sealed=True, limits=api.CowrieTtyLimits(max_bytes=budget)
    )
    assert result.code == "BYTES_LIMIT"
    assert stream.tell() == budget + 1
    assert result.consumed_bytes == (24 if budget >= 24 else 0)


def test_exact_frame_and_payload_limits(api):
    data = frame(1) + frame(3, b"abc", direction=2) + frame(2)
    assert convert(api, data, limits=api.CowrieTtyLimits(max_frames=3, max_payload=3)).complete
    result = convert(api, data, limits=api.CowrieTtyLimits(max_payload=2))
    assert result.code == "PAYLOAD_LIMIT" and result.consumed_bytes == 24
    assert convert(api, frame(1), limits=api.CowrieTtyLimits(max_frames=1)).code == "MISSING_CLOSE"


def test_io_failure_preserves_accepted_prefix(api):
    prefix = frame(1) + frame(3, b"good", direction=2)

    class BrokenAfterPrefix(ShortReads):
        def read(self, size=-1):
            if self.tell() == len(prefix):
                raise OSError("SECRET")
            return super().read(size)

    result = api.CowrieTtyConverter().convert(BrokenAfterPrefix(prefix), sealed=True)
    assert result.code == "IO_ERROR" and result.consumed_bytes == len(prefix)
    assert result.chunks == (chunk(0, 0, "out", b"good"),)
    assert "SECRET" not in repr(result)


def test_closed_stream_is_controlled_error(api):
    stream = io.BytesIO()
    stream.close()
    assert api.CowrieTtyConverter().convert(stream).code == "IO_ERROR"


def test_uint32_time_and_generated_chunks_fit_replay_schema(api):
    data = frame(1, sec=0) + frame(3, b"\xff\x00", direction=2, sec=2**32 - 1)
    data += frame(2, sec=2**32 - 1)
    result = convert(api, data)
    assert result.complete and result.chunks[0]["offset_ms"] == (2**32 - 1) * 1000
    schema = json.loads((ROOT / "contracts/replay-chunk-v1.schema.json").read_text("utf-8"))
    for value in result.chunks:
        jsonschema.Draft202012Validator(schema).validate(value)
