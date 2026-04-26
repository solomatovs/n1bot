"""Конфигурация CLI.

Источники (в порядке приоритета): CLI-флаг > env > error.

``CHROMA_PERSIST_PATH`` — общая env-переменная: тот же путь читает
``boba-ext-chromadb`` для read-tools агента. Оператор задаёт её один
раз и видит свежепроиндексированные коллекции в chainlit/agent-cli без
повторного указания путей.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PERSIST_PATH_ENV = "CHROMA_PERSIST_PATH"


class CliConfigError(Exception):
    """Ошибка конфига: например, не указан persist_path ни флагом, ни env."""


@dataclass(frozen=True)
class CliConfig:
    persist_path: str

    @classmethod
    def resolve(cls, *, persist_path_arg: str | None) -> CliConfig:
        """Собирает конфиг из CLI-аргумента и env. Бросает
        :class:`CliConfigError` если ни один источник не задал
        обязательное поле.
        """
        persist_path = persist_path_arg or os.environ.get(PERSIST_PATH_ENV)
        if not persist_path:
            raise CliConfigError(
                f"persist_path is required: pass --persist-path "
                f"or set {PERSIST_PATH_ENV} env var"
            )
        return cls(persist_path=persist_path)
