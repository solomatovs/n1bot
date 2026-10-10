"""Порт истории агента: захват сессии, атомарная дозапись пачки, чтение по порядку.

Реализации — пакеты boba-history-jsonl и boba-history-postgres; ядро знает
только этот протокол. Строка, которую хранилище не может разобрать как JSON,
отдаётся читателю как BrokenLine: решение о ней принимает загрузчик.

Ошибки:
HistoryError — сессия занята другим процессом, хранилище недоступно или
    отказало; причина нижнего слоя — в тексте и в `from exc`.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

from boba.agent.records import Record

__all__ = [
    "BrokenLine",
    "HistoryError",
    "HistoryLine",
    "HistoryStore",
    "SessionId",
    "SessionLease",
    "StoredLine",
]


class HistoryError(Exception):
    """Хранилище истории недоступно, занято или отказало."""


class SessionId(BaseModel):
    """Идентификатор сессии: безопасен как имя файла и ключ таблицы."""

    model_config = ConfigDict(frozen=True)

    SHAPE: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

    value: str

    @field_validator("value")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        if not cls.SHAPE.match(value):
            msg = (
                f"'{value}' is not a valid session id. The id names the journal file, "
                "so it must be 1 to 128 characters: letters, digits, '-' or '_'."
            )
            raise ValueError(msg)

        return value


@dataclass(frozen=True)
class SessionLease:
    """Захваченная сессия: по ключу хранилище узнаёт свои ресурсы захвата."""

    session: SessionId
    key: str


@dataclass(frozen=True)
class StoredLine:
    """Разобранная строка журнала: номер строки с 1 и JSON-объект."""

    number: int
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class BrokenLine:
    """Строка журнала, которая не разбирается как JSON-объект."""

    number: int
    reason: str


HistoryLine: TypeAlias = StoredLine | BrokenLine


class HistoryStore(Protocol):
    """Хранилище записей сессии."""

    @abstractmethod
    async def acquire(self, session: SessionId) -> SessionLease:
        """Захватывает сессию за этим процессом; второй захват — HistoryError.
        Сессии без журнала создаётся пустой."""

    @abstractmethod
    async def append(self, lease: SessionLease, records: Sequence[Record]) -> None:
        """Дописывает пачку целиком в порядке seq; записи с уже сохранённым
        seq пропускаются, повтор пачки дублей не создаёт."""

    @abstractmethod
    def read(self, session: SessionId) -> AsyncIterator[HistoryLine]:
        """Строки журнала в порядке seq с первой до последней."""

    @abstractmethod
    async def release(self, lease: SessionLease) -> None:
        """Отпускает сессию."""
