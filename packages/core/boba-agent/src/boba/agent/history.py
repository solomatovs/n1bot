"""Порт истории агента: захват сессии, атомарная дозапись пачки, чтение по
порядку, список сессий страницами и удаление сессии.

Реализации — пакеты boba-history-jsonl и boba-history-postgres; ядро знает
только этот протокол. Здесь же живёт MemoryHistoryStore — реализация без
хранилища: записи хода остаются только в Session процесса. Строка, которую
хранилище не может разобрать как JSON, отдаётся читателю как BrokenLine:
решение о ней принимает загрузчик.

Ошибки:
HistoryError — хранилище недоступно или отказало; причина нижнего слоя — в
    тексте и в `from exc`.
SessionHeldError — сессию держит другой процесс: её замок занят; подвид
    HistoryError.
SessionNotFoundError — у сессии нет ни одной записи, возобновлять нечего;
    подвид HistoryError.
"""

from __future__ import annotations

import re
import uuid
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

from boba.agent.records import Record

__all__ = [
    "BrokenLine",
    "HistoryError",
    "HistoryLine",
    "HistoryStore",
    "LineKey",
    "MemoryHistoryStore",
    "SessionHeldError",
    "SessionId",
    "SessionLease",
    "SessionNotFoundError",
    "StoredLine",
    "StoredPage",
    "StoredSession",
]


class HistoryError(Exception):
    """Хранилище истории недоступно, занято или отказало."""


class SessionHeldError(HistoryError):
    """Сессию держит другой процесс: её замок в хранилище занят."""


class SessionNotFoundError(HistoryError):
    """У сессии нет записей в хранилище: возобновлять нечего."""


class LineKey(StrEnum):
    """Ключи документа записи, которые хранилище читает само, не разбирая
    запись в модель: порядковый номер и время записи."""

    SEQ = "seq"
    TIMESTAMP = "timestamp"


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


@dataclass(frozen=True)
class StoredSession:
    """Сессия в хранилище: идентификатор и время последней записи (ISO 8601)."""

    session: SessionId
    updated_at: str


@dataclass(frozen=True)
class StoredPage:
    """Страница списка сессий по возрастанию идентификатора; `next_cursor` —
    идентификатор последней сессии страницы, с которого продолжать, None —
    страница последняя."""

    sessions: Sequence[StoredSession]
    next_cursor: str | None


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

    @abstractmethod
    async def list(self, cursor: str | None, limit: int) -> StoredPage:
        """Сессии с записями по возрастанию идентификатора: не больше `limit`
        после `cursor` (идентификатор сессии; None — с начала)."""

    @abstractmethod
    async def delete(self, session: SessionId) -> None:
        """Удаляет записи сессии; сессии без записей — не ошибка.

        Ошибки:
        SessionHeldError — сессию держит работающий процесс.
        """


class MemoryHistoryStore(HistoryStore):
    """Реализация HistoryStore без хранилища.

    Нужна процессу, у которого история живёт только в Session в памяти:
    хранилище снаружи ему не положено, но порт обязателен. Захват сессии
    замка не ставит, дозапись ничего не пишет, чтение всегда пусто, список
    сессий пуст, удалять нечего — после перезапуска процесса истории нет,
    возобновить сессию нельзя.
    """

    async def acquire(self, session: SessionId) -> SessionLease:
        return SessionLease(session=session, key=uuid.uuid4().hex)

    async def append(self, lease: SessionLease, records: Sequence[Record]) -> None:
        return

    async def read(self, session: SessionId) -> AsyncIterator[HistoryLine]:
        nothing: Sequence[HistoryLine] = ()
        for line in nothing:
            yield line

    async def release(self, lease: SessionLease) -> None:
        return

    async def list(self, cursor: str | None, limit: int) -> StoredPage:
        return StoredPage(sessions=(), next_cursor=None)

    async def delete(self, session: SessionId) -> None:
        return
