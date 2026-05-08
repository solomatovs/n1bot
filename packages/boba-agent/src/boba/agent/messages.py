"""Порт хранения истории сообщений (Reader/Writer split)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Self

from boba.agent.events import AgentEvent, PersistenceFailed
from boba.agent.state import ChannelId, StateChannel
from boba.errors import TerminalError
from boba.llm.models import Message, RequestId

__all__ = [
    "MessageReader",
    "MessageService",
    "MessageStoreError",
    "MessageStoreReadError",
    "MessageStoreWriteError",
    "MessageWriter",
]


class MessageStoreError(TerminalError[RequestId, AgentEvent]):
    """Базовая ошибка persistent-реализации MessageService."""

    def __init__(self, reason: Exception, ctx: str = "") -> None:
        self.reason = reason
        self.ctx = ctx
        prefix = self._prefix()
        msg = f"{prefix}: {reason}"
        if ctx:
            msg = f"{prefix} ({ctx}): {reason}"
        super().__init__(msg)

    def _prefix(self) -> str:
        return "Message store error"

    def to_user_feedback(self, request_id: RequestId) -> PersistenceFailed:
        return PersistenceFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
        )


class MessageStoreWriteError(MessageStoreError):
    """Не удалось записать сообщение в хранилище."""

    def _prefix(self) -> str:
        return "Cannot write message"


class MessageStoreReadError(MessageStoreError):
    """Не удалось прочитать хранилище сообщений."""

    def _prefix(self) -> str:
        return "Cannot read messages"


class MessageReader(ABC):
    """Read-side порт хранилища сообщений."""

    @abstractmethod
    def message_iter(self) -> Iterator[Message]:
        """Все сообщения в порядке добавления; MessageStoreReadError при сбое."""
        ...

    @abstractmethod
    def last(self) -> Message | None:
        """Последнее сообщение или None."""
        ...


class MessageWriter(ABC):
    """Write-side порт хранилища сообщений."""

    @abstractmethod
    def add(self, message: Message) -> None:
        """Добавить сообщение; MessageStoreWriteError при сбое."""
        ...

    def add_many(self, messages: Iterable[Message]) -> None:
        """Bulk-добавление; default — цикл add. Persistent impls могут оверрайдить."""
        for message in messages:
            self.add(message)

    @abstractmethod
    def clear(self) -> None:
        """Очистить историю; MessageStoreWriteError при сбое."""
        ...


class MessageService(MessageReader, MessageWriter, StateChannel, ABC):
    """Композиция MessageReader + MessageWriter + StateChannel."""

    @classmethod
    def channel_id(cls) -> ChannelId[Self]:
        return ChannelId("messages")

class InMemoryMessageService(MessageService):
    """In-memory реализация MessageService."""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    def add(self, message: Message) -> None:
        self._messages.append(message)

    def message_iter(self) -> Iterator[Message]:
        return iter(self._messages)

    def last(self) -> Message | None:
        return self._messages[-1] if self._messages else None

    def clear(self) -> None:
        self._messages.clear()
