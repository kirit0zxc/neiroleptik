"""Разбор аргументов командной строки Low и нормализация терминаторов строк."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

import honeypots.low.__main__ as low_main
from honeypots.low import LowConfig
from honeypots.low.__main__ import build_parser, config_from_args, journal_from_args
from honeypots.low.journal import JournalSettings, RotatingJournalSink


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


# --- Параметры файлового журнала и публичные порты -------------------------------------

INSTANCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEPLOYMENT = "11111111-1111-4111-8111-111111111111"


def journal(*argv: str) -> object:
    return journal_from_args(build_parser().parse_args(argv))


def test_public_ports_reach_configuration() -> None:
    result = config("--tcp-public-port=2200", "--http-public-port=8800")

    assert (result.tcp_public_port, result.http_public_port) == (2200, 8800)
    assert config().tcp_public_port is None


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_invalid_public_port_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        config(f"--http-public-port={value}")


def test_without_journal_dir_there_is_no_file_journal() -> None:
    assert journal() is None
    assert journal(f"--instance-id={INSTANCE}") is None


def test_journal_options_build_settings() -> None:
    settings = journal(
        "--journal-dir=/journal",
        f"--instance-id={INSTANCE}",
        f"--deployment-id={DEPLOYMENT}",
        "--profile-revision=4",
        "--journal-max-bytes=300000",
        "--journal-quota-bytes=900000",
        "--journal-fsync",
    )

    assert isinstance(settings, JournalSettings)
    assert settings.directory == Path("/journal")
    assert (settings.instance_id, settings.deployment_id) == (INSTANCE, DEPLOYMENT)
    assert settings.profile_revision == 4
    assert (settings.max_file_bytes, settings.max_total_bytes) == (300000, 900000)
    assert settings.fsync_each_event is True


@pytest.mark.parametrize(
    "missing",
    ["--instance-id", "--deployment-id", "--profile-revision"],
)
def test_journal_dir_requires_all_identifiers(missing: str) -> None:
    arguments = {
        "--instance-id": INSTANCE,
        "--deployment-id": DEPLOYMENT,
        "--profile-revision": "1",
    }
    del arguments[missing]

    with pytest.raises(ValueError, match="требует"):
        journal("--journal-dir=/journal", *(f"{key}={value}" for key, value in arguments.items()))


def test_config_from_args_ignores_journal_only_options() -> None:
    assert config("--journal-dir=/journal", f"--instance-id={INSTANCE}") == LowConfig()


# --- Ошибки запуска ---------------------------------------------------------------------


def test_occupied_port_gives_a_readable_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Регрессия: занятый порт давал трассировку вместо понятного сообщения."""
    import socket

    from honeypots.low.__main__ import main

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        with pytest.raises(SystemExit) as exit_info:
            main([f"--tcp-port={port}", "--http-port=0"])

    assert exit_info.value.code == 1
    error = capsys.readouterr().err
    assert "не удалось запустить сервис" in error
    assert "Traceback" not in error


def test_missing_journal_directory_gives_a_readable_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from honeypots.low.__main__ import main

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--tcp-port=0",
                "--http-port=0",
                f"--journal-dir={tmp_path / 'absent'}",
                f"--instance-id={INSTANCE}",
                f"--deployment-id={DEPLOYMENT}",
                "--profile-revision=1",
            ]
        )

    assert exit_info.value.code == 1
    assert "не удалось запустить сервис" in capsys.readouterr().err


def test_stop_handlers_are_installed_before_the_journal_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регрессия: SIGTERM между открытием журнала и установкой обработчиков убивал процесс
    действием по умолчанию и оставлял файл без концовки."""
    order: list[str] = []

    def install(loop: asyncio.AbstractEventLoop, stopping: asyncio.Event) -> Callable[[], None]:
        order.append("handlers")
        return lambda: order.append("restore")

    def failing_open(self: RotatingJournalSink) -> None:
        order.append("open")
        raise OSError("каталог журнала недоступен")

    monkeypatch.setattr(low_main, "_install_stop_handlers", install)
    monkeypatch.setattr(RotatingJournalSink, "open", failing_open)
    journal = JournalSettings(
        directory=tmp_path,
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        deployment_id="11111111-1111-4111-8111-111111111111",
        profile_revision=1,
    )

    with pytest.raises(OSError):
        asyncio.run(low_main._run(LowConfig(tcp_port=0, http_port=0), journal))

    assert order == ["handlers", "open", "restore"]
