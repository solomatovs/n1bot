"""Порт хранения истории сообщений (Reader/Writer split) + реализации."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Self

from pydantic import ValidationError

from boba.agent.errors import TerminalError
from boba.agent.events import AgentEvent, PersistenceFailed
from boba.agent.state import ChannelId, StateChannel
from boba.llm.models import Message, MessageAdapter, RequestId
from boba.workspace.contract import HistoryWorkspaceShell, WorkspaceError

__all__ = [
    "InMemoryMessageService",
    "JsonLinesMessageService",
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


class JsonLinesMessageService(MessageService):
    """Journaling-реализация MessageService поверх workspace-файла."""

    _DEFAULT_FILENAME = "messages.jsonl"

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        filename: str = _DEFAULT_FILENAME,
    ) -> None:
        self._workspace = workspace
        self._filename = filename
        self._messages: list[Message] = []
        self._ensure_file()
        self._load()

    def _ensure_file(self) -> None:
        try:
            if self._workspace.exists(self._filename):
                return
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc

    def _load(self) -> None:
        try:
            for line in self._workspace.read_lines(self._filename):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    self._messages.append(MessageAdapter.validate_json(stripped))
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise MessageStoreReadError(
                        exc, ctx=f"path={self._filename}: {stripped!r}",
                    ) from exc
        except WorkspaceError as exc:
            raise MessageStoreReadError(exc, ctx=f"path={self._filename}") from exc

    def add(self, message: Message) -> None:
        line = MessageAdapter.dump_json(message).decode("utf-8")
        try:
            with self._workspace.append_text(self._filename) as f:
                f.write(line)
                f.write("\n")
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc
        self._messages.append(message)

    def message_iter(self) -> Iterator[Message]:
        return iter(self._messages)

    def last(self) -> Message | None:
        return self._messages[-1] if self._messages else None

    def clear(self) -> None:
        try:
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise MessageStoreWriteError(exc, ctx=f"path={self._filename}") from exc
        self._messages.clear()
