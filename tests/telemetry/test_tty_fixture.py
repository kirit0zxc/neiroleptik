"""T01–T04: собственный versioned fixture, не реальные журналы Cowrie."""

import io
import struct

import pytest

from agent.telemetry.tty_fixture import FixtureTtyConverter, TtyLimits


def frame(timestamp, direction, payload):
    # Независимое построение bytes по спецификации, без импорта parser HEADER.
    return struct.pack("!QBI", timestamp, direction, len(payload)) + payload


class Chunked(io.BytesIO):
    def read(self, size=-1):
        assert 0 <= size <= 4096
        return super().read(min(size, 1))


def convert(payload, limits=None):
    return FixtureTtyConverter().convert(Chunked(payload), limits)


def test_ascii_unicode_split_frames_direction_and_controls():
    encoded = "я".encode()
    data = b"HF-TTY/1\n" + frame(0, 1, b"cmd\n") + frame(0, 2, encoded[:1])
    data += frame(1, 1, b"<script>\x1b[31m\r\x7f") + frame(2, 2, encoded[1:])
    result = convert(data)
    assert not result.incomplete and result.code is None
    assert [(f.timestamp_us, f.direction, f.text) for f in result.fragments] == [
        (0, "input", "cmd\n"),
        (0, "output", ""),
        (1, "input", "<script>\\x1b[31m\\x0d\\x7f"),
        (2, "output", "я"),
    ]


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", "EMPTY_INPUT"),
        (b"wrong", "INVALID_MAGIC"),
        (b"HF-TTY/1\n\x00", "INCOMPLETE_HEADER"),
        (b"HF-TTY/1\n" + frame(0, 1, b"abc")[:-1], "INCOMPLETE_PAYLOAD"),
        (b"HF-TTY/1\n" + frame(0, 9, b""), "UNKNOWN_DIRECTION"),
        (b"HF-TTY/1\n" + frame(0, 1, b"\xff"), "INVALID_UTF8"),
        (b"HF-TTY/1\n" + frame(0, 1, b"\xd1"), "INVALID_UTF8"),
        (b"HF-TTY/1\n" + frame(1, 1, b"a") + frame(0, 1, b"b"), "REVERSED_TIME"),
    ],
)
def test_explicit_incomplete(data, code):
    result = convert(data)
    assert result.incomplete and result.code == code


def test_empty_recording_and_equal_timestamps():
    assert not convert(b"HF-TTY/1\n").incomplete
    result = convert(b"HF-TTY/1\n" + frame(9, 1, b"a") + frame(9, 1, b"b"))
    assert [f.text for f in result.fragments] == ["a", "b"]


def test_limits_do_not_read_unbounded_payload():
    assert convert(b"HF-TTY/1\n" + struct.pack("!QBI", 0, 1, 2**32 - 1)).code == "PAYLOAD_LIMIT"
    sample = b"HF-TTY/1\n" + frame(0, 1, b"a") + frame(1, 1, b"b")
    assert convert(sample, TtyLimits(max_bytes=20)).code == "BYTES_LIMIT"
    assert convert(sample, TtyLimits(max_frames=1)).code == "FRAME_LIMIT"
    assert not convert(sample, TtyLimits(max_bytes=len(sample))).incomplete


def test_invalid_budget():
    with pytest.raises(ValueError):
        TtyLimits(max_payload=True)
