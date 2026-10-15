"""Выделяет постоянное состояние одному instance и запускает настоящий Cowrie."""

import json
import os
import resource
import sys
from pathlib import Path
from uuid import UUID


class CowrieLauncher:
    """Повтор процесса в прежнем instance не разрешён: каталог создаётся эксклюзивно."""

    def run(self, instance: str) -> None:
        if sys.platform != "linux":
            raise RuntimeError("Cowrie запускается только внутри Linux-контейнера")
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
        identifier = str(UUID(instance))
        profile = json.loads(Path("/honeyforge-config/profile.json").read_text(encoding="utf-8"))
        root = Path("/cowrie/cowrie-git/var") / identifier
        root.mkdir(mode=0o700, exist_ok=False)
        for name in ("log", "state", "etc", "downloads", "tty"):
            (root / name).mkdir(mode=0o700)
        # Hex-regex означает точный UTF-8 пароль: '*' и '!' не становятся правилами Cowrie.
        users = []
        for item in profile["credentials"]:
            password = "".join(f"\\x{value:02x}" for value in item["password"].encode("utf-8"))
            users.append(f"{item['username']}:x:/\\A{password}\\Z/")
        (root / "etc" / "userdb.txt").write_text("\n".join(users) + "\n", encoding="ascii")
        values = {
            "HONEYPOT_LOGTYPE": "plain",
            "HONEYPOT_LOG_PATH": str(root / "log"),
            "HONEYPOT_STATE_PATH": str(root / "state"),
            "HONEYPOT_ETC_PATH": str(root / "etc"),
            "HONEYPOT_DOWNLOAD_PATH": str(root / "downloads"),
            "HONEYPOT_TTYLOG_PATH": str(root / "tty"),
            "HONEYPOT_HOSTNAME": profile["hostname"],
            "SSH_VERSION": profile["banner"] or "SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3",
            "SSH_ENABLED": "true",
            "SSH_FORWARDING": "false",
            "SSH_SFTP_ENABLED": "false",
            "TELNET_ENABLED": "false",
            "OUTPUT_JSONLOG_ENABLED": "true",
            "OUTPUT_JSONLOG_LOGFILE": str(root / "log" / "cowrie.json"),
        }
        os.environ.update({"COWRIE_" + key: value for key, value in values.items()})
        os.execv(
            sys.executable,
            [
                sys.executable,
                "/cowrie/cowrie-env/bin/twistd",
                "-n",
                "--umask=0077",
                "--pidfile=",
                "--logger",
                "cowrie.python.logfile.stdoutLogger",
                "cowrie",
            ],
        )


if __name__ == "__main__":
    CowrieLauncher().run(sys.argv[1])
