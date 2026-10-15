"""Конфигурация Cowrie передаётся закрытым файлом, а не аргументами процесса."""

import json
import os
import stat
from pathlib import Path

from agent.runtime.contract import ProfileSpec


class MediumConfigStore:
    """Неизменяемая конфигурация одного instance на доверенном диске оператора."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, deployment: str, instance: str, profile: ProfileSpec) -> Path:
        """Повтор допустим только с прежними байтами; ссылки и чужие файлы запрещены."""
        directory = self.root / deployment / instance
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        for parent in (directory, *directory.parents):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Конфигурация Medium не может проходить через ссылку")
        data = json.dumps(
            {
                "hostname": profile.hostname,
                "banner": profile.services[0].banner,
                "port": profile.services[0].port,
                "credentials": [
                    {"username": item.username, "password": item.password.reveal()}
                    for item in profile.credentials
                ],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        path = directory / "profile.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Небезопасный файл конфигурации Medium") from None
            if path.read_bytes() != data:
                raise ValueError("Конфигурация instance уже зафиксирована") from None
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        # Linux-оператор должен иметь право назначить каталог пользователю Cowrie.
        if hasattr(os, "chown"):
            os.chown(path, 999, 999)
            os.chown(directory, 999, 999)
        return directory
