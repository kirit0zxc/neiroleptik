"""Request проекция не допускает секретов, поддельной полноты и неизвестного method."""

from copy import deepcopy

import pytest

from honeyforge.telemetry.masking import PublicPolicy


@pytest.mark.parametrize("kind", ["http.request", "tcp.request"])
def test_request_allowlist(kind):
    raw = {
        "payload_b64": "c2VjcmV0",
        "body": "secret",
        "headers": {"Cookie": "secret"},
        "path": "/secret?token=secret",
        "method": "GET",
        "status": 200,
        "low_seq": "99",
        "captured_bytes": 6,
        "mapper_truncated": False,
        "capture_completeness": "unknown",
        "http_parse_state": "parsed",
        "truncated": False,
        "extra": "secret",
    }
    original = deepcopy(raw)
    actual = PublicPolicy()({"type": kind, "data": raw})["data"]
    expected = {
        "captured_bytes": 6,
        "mapper_truncated": False,
        "capture_completeness": "unknown",
    }
    if kind == "http.request":
        expected |= {"method": "GET", "path": "[скрыто]", "http_parse_state": "parsed"}
    assert actual == expected
    assert raw == original


@pytest.mark.parametrize("value", [None, True, {}, [], -1, 1.5, "secret"])
def test_invalid_capture_and_method_are_omitted(value):
    raw = dict.fromkeys(["captured_bytes", "method", "http_parse_state"], value)
    assert PublicPolicy()({"type": "http.request", "data": raw})["data"] == {}


def test_root_only_and_false_completeness_hidden():
    for path in ["/?secret=x", "/#secret", "/password", ""]:
        actual = PublicPolicy()({"type": "http.request", "data": {"path": path}})["data"]
        assert actual == {"path": "[скрыто]"}
    actual = PublicPolicy()(
        {
            "type": "http.request",
            "data": {"path": "/", "capture_completeness": "confirmed", "method": "BOGUS"},
        }
    )["data"]
    assert actual == {"path": "/"}
