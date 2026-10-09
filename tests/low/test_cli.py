"""Разбор аргументов командной строки Low и нормализация терминаторов строк."""

import pytest

from honeypots.low import LowConfig
from honeypots.low.__main__ import build_parser, config_from_args


def config(*argv: str) -> LowConfig:
    return config_from_args(build_parser().parse_args(argv))


def test_defaults_equal_low_config_defaults() -> None:
    assert config() == LowConfig()


def test_banner_without_terminator_receives_crlf() -> None:
    assert config("--banner=SSH-2.0-OpenSSH_9.6").banner == "SSH-2.0-OpenSSH_9.6\r\n"


def test_banner_with_crlf_is_not_duplicated() -> None:
    assert config("--banner=SSH-2.0-X\r\n").banner == "SSH-2.0-X\r\n"


def test_banner_may_start_with_a_dash_when_given_with_equals_sign() -> None:
    assert config("--banner=-dash").banner == "-dash\r\n"


@pytest.mark.parametrize("banner", ["", "a\nb", "Сервис", "a" * 300])
def test_invalid_banner_is_still_rejected_after_normalization(banner: str) -> None:
    with pytest.raises(ValueError):
        config(f"--banner={banner}")


def test_http_body_receives_a_single_trailing_newline() -> None:
    assert config("--http-body=Welcome").http_body == "Welcome\n"
    assert config("--http-body=Welcome\n").http_body == "Welcome\n"


def test_empty_http_body_stays_empty() -> None:
    assert config("--http-body=").http_body == ""


def test_numeric_options_reach_configuration() -> None:
    result = config(
        "--host=0.0.0.0",
        "--tcp-port=2200",
        "--http-port=8800",
        "--max-input-bytes=512",
        "--max-connections=7",
        "--read-timeout=1.5",
    )

    assert (result.host, result.tcp_port, result.http_port) == ("0.0.0.0", 2200, 8800)
    assert (result.max_input_bytes, result.max_connections, result.read_timeout) == (512, 7, 1.5)
