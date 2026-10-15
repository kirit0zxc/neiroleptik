"""Общие части Linux-проверок доверенной границы процесса Low.

Здесь собрано то, что иначе расходилось бы между файлами проверок: требование
Linux, группа чтения collector и безопасная база для корней публикации.
"""

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4


def require_linux(subject: str) -> None:
    """Шаг CI с HF_REQUIRE_LINUX_BOUNDARY=1 требует фактического выполнения проверок.

    Переменная защищает от запуска на другой ОС, где публикация выключена и проверки
    молча пропустились бы. Факт их выполнения на Linux она не доказывает.
    """
    if sys.platform != "linux" and os.environ.get("HF_REQUIRE_LINUX_BOUNDARY") == "1":
        raise AssertionError(f"Проверке {subject} необходим Linux")


def collector_gid() -> int:
    """Группа чтения collector для проверки механизма публикации.

    В production группу G задаёт оператор, а POSIX разрешает менять группу файла
    только владельцу и только на свою или дополнительную группу. Поэтому под root
    используется распространённый nonroot-gid, а под обычным пользователем — группа
    самого процесса: иначе проверки падали бы по правам там, где механизм исправен
    (именно так падал бы шаг CI, который работает не от root). Раздельные
    пользователи R, C и L этим не проверяются и остаются в §9.4 как непроверенные.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return 65532
    getgid = getattr(os, "getgid", None)
    return 0 if getgid is None else getgid()


def publication_base(prefix: str) -> Iterator[Path]:
    """Безопасная база корней публикации: без записи группе и остальным (§8.2).

    База берётся не из /tmp: он доступен на запись остальным, и такой родитель
    корня по §8.2 сам по себе запрещён. Путь задаётся HF_BOUNDARY_TEST_ROOT либо
    домашним каталогом пользователя.
    """
    home = Path(os.environ.get("HF_BOUNDARY_TEST_ROOT", str(Path.home())))
    root = home / f".honeyforge-{prefix}-{uuid4()}"
    root.mkdir(mode=0o750, parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
