"""Конфигурация CLI.

Источники (в порядке приоритета): CLI-флаги > env > defaults.

Конвенция env-имён: ``BOBA_VECTOR_INDEX_*`` — собственный namespace CLI,
не пересекающийся с агентскими ``BOBA_EXT_CHROMADB__*``. Оператор
обычно прописывает оба указывающими на тот же ``persist_path``, чтобы
агент читал то, что CLI индексирует, — но конкретное согласование
оставлено оператору, не делается автоматически.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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
        persist_path = persist_path_arg or os.environ.get(
            "BOBA_VECTOR_INDEX_PERSIST_PATH"
        )
        if not persist_path:
            raise CliConfigError(
                "persist_path is required: pass --persist-path "
                "or set BOBA_VECTOR_INDEX_PERSIST_PATH env var"
            )
        return cls(persist_path=persist_path)
