"""Идентификаторы и время записей: то, что агент чеканит сам.

Идентификаторы: uuid записи — случайный UUID4,
идентификатор ответа — `msg_` и 24 знака, запроса — `req_` и 24 знака,
вызова инструмента — 9 знаков из [a-zA-Z0-9] (раздел 5.12 плана).
Время — ISO 8601 с миллисекундами в UTC
"""

from __future__ import annotations

import secrets
import string
import uuid
from abc import abstractmethod
from datetime import UTC, datetime
from typing import ClassVar, Protocol

__all__ = ["Clock", "IdMint", "SystemClock"]


class Clock(Protocol):
    """Источник времени записей."""

    @abstractmethod
    def now(self) -> str:
        """Текущее время строкой журнала: `2026-10-10T12:00:00.000Z`."""


class SystemClock(Clock):
    """Реализация Clock по системным часам."""

    FORMAT: ClassVar[str] = "%Y-%m-%dT%H:%M:%S"

    def now(self) -> str:
        moment = datetime.now(UTC)
        millis = moment.microsecond // 1000

        return f"{moment.strftime(self.FORMAT)}.{millis:03d}Z"


class IdMint:
    """Чеканка идентификаторов записей, ответов, запросов и вызовов."""

    ALPHABET: ClassVar[str] = string.ascii_letters + string.digits
    MESSAGE_PREFIX: ClassVar[str] = "msg_"
    REQUEST_PREFIX: ClassVar[str] = "req_"
    LONG: ClassVar[int] = 24
    CALL: ClassVar[int] = 9

    def uuid(self) -> str:
        return str(uuid.uuid4())

    def message_id(self) -> str:
        return self.MESSAGE_PREFIX + self._token(self.LONG)

    def request_id(self) -> str:
        return self.REQUEST_PREFIX + self._token(self.LONG)

    def call_id(self) -> str:
        return self._token(self.CALL)

    def _token(self, length: int) -> str:
        letters: list[str] = []
        for _ in range(length):
            letters.append(secrets.choice(self.ALPHABET))

        return "".join(letters)
