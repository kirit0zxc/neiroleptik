"""Проверка CI по разрешённым результатам, включая сбои выдачи runner."""

import os
import sys


class CiResultPolicy:
    """Пропуск допустим лишь для отсутствующего компонента."""

    @classmethod
    def validate(cls, env):
        if env.get("REPOSITORY_RESULT") != "success":
            return "Repository checks должны завершиться успешно."
        for component in ("FRONTEND", "BACKEND"):
            active = env.get(f"{component}_ACTIVE")
            result = env.get(f"{component}_RESULT")
            if active not in ("true", "false"):
                return f"{component}: не определено наличие компонента."
            allowed = ("success",) if active == "true" else ("success", "skipped")
            if result not in allowed:
                return f"{component}: недопустимый результат {result!r}."
        return None


if __name__ == "__main__":
    error = CiResultPolicy.validate(os.environ)
    if error:
        print(f"::error::{error}")
        sys.exit(1)
    print("Все обязательные проверки успешно завершены.")
