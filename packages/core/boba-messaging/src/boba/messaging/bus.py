"""Порт шины сообщений: публикация в область, подписка на область, команды, replay;
реализации — Postgres и память с одним контрактом.

Ошибки:
MessageTooLargeError — тело сообщения больше BusLimit.BODY_MAX_BYTES.
LockLostError — публикация от имени отобранной блокировки (boba.identity.locks).
ListenerFailedError — подписчик не справился с конвертом; остальные подписчики
    конверт получили, ошибка каждого приложена.
MessageBusError — реализация не смогла сохранить или прочитать сообщение.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from enum import IntEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.context import Scope
from boba.identity.locks import LockLostError, LockToken
from boba.messaging.messages import AnyCommand, AnyMessage, Message, StreamAppended

__all__ = [
    "BusLimit",
    "CommandEnvelope",
    "CommandListener",
    "Envelope",
    "Listener",
    "ListenerFailedError",
    "LockLostError",
    "LockToken",
    "MessageBus",
    "MessageBusError",
    "MessageTooLargeError",
    "StreamFeed",
    "Unsubscribe",
]

logger = logging.getLogger(__name__)


class MessageBusError(Exception):
    """Шина не смогла сохранить, доставить или прочитать сообщение."""


class MessageTooLargeError(MessageBusError):
    """Тело сообщения больше предела одного сообщения шины."""


class ListenerFailedError(MessageBusError):
    """Подписчик не справился с конвертом; конверт при этом дошёл до остальных, а
    ошибки всех упавших подписчиков приложены.
    """

    def __init__(self, what: str, failures: Sequence[BaseException]) -> None:
        described: list[str] = []
        for failure in failures:
            described.append(f"{type(failure).__name__}: {failure}")

        super().__init__(f"{what}: {'; '.join(described)}")
        self.failures = tuple(failures)


class BusLimit(IntEnum):
    """Пределы шины в байтах: тело сообщения должно влезать в уведомление Postgres
    вместе с обвязкой.
    """

    BODY_MAX_BYTES = 7000
    NOTIFY_MAX_BYTES = 8000


class Envelope(BaseModel):
    """Одно сообщение шины вместе с тем, где и когда оно появилось."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope
    seq: int = Field(ge=1)
    at: datetime
    origin: str = Field(min_length=1)
    message: AnyMessage

    @model_validator(mode="after")
    def _body_fits(self) -> Self:
        size = self.body_size(self.message)
        if size > BusLimit.BODY_MAX_BYTES:
            msg = (
                f"message {self.message.kind} of {size} bytes exceeds "
                f"{BusLimit.BODY_MAX_BYTES} bytes"
            )
            raise ValueError(msg)

        return self

    @staticmethod
    def body_size(message: Message) -> int:
        return len(message.model_dump_json().encode("utf-8"))


class CommandEnvelope(BaseModel):
    """Команда области вместе с её номером и моментом отправки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope
    command_id: int = Field(ge=1)
    at: datetime
    command: AnyCommand


Listener = Callable[[Envelope], Awaitable[None]]
CommandListener = Callable[[CommandEnvelope], Awaitable[None]]
Unsubscribe = Callable[[], None]


class StreamFeed(Protocol):
    """Приёмник сообщений о росте журнала вызова: запуск отдаёт их владельцу,
    который публикует их в свою область от имени держателя.
    """

    @abstractmethod
    async def stream_appended(self, message: StreamAppended) -> None:
        """Сообщает получателям, что канал журнала вызова дорос до size байт."""


class MessageBus(Protocol):
    """Единственный путь сообщений между исполнителем и получателями: публикация от
    держателя области, команды от кого угодно, доставка в порядке seq.
    """

    @abstractmethod
    async def publish(self, scope: Scope, message: AnyMessage, token: LockToken) -> int:
        """Публикует сообщение от имени держателя области."""

    @abstractmethod
    async def command(self, scope: Scope, command: AnyCommand) -> int:
        """Отправляет команду области от любого процесса."""

    @abstractmethod
    def subscribe(self, scope: Scope, listener: Listener) -> Unsubscribe:
        """Подписывает получателя на все последующие сообщения области."""

    @abstractmethod
    def subscribe_commands(self, listener: CommandListener) -> Unsubscribe:
        """Подписывает процесс на команды всех областей."""

    @abstractmethod
    async def replay(self, scope: Scope, after_seq: int) -> Sequence[Envelope]:
        """Возвращает сохранённые сообщения области с номерами больше after_seq."""

    @abstractmethod
    async def take(self, scope: Scope, command_id: int, instance: str) -> bool:
        """Забирает команду на исполнение от имени инстанса."""

    @abstractmethod
    async def purge(self, scope: Scope) -> int:
        """Удаляет сохранённые сообщения и команды завершённой области."""
