"""Порт журнала AgentEvent (Reader/Writer split) + реализации (in-memory, JSONL)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Self

from pydantic import ValidationError

from boba.agent.errors import TerminalError
from boba.agent.events import (
    AgentEvent,
    AgentEventAdapter,
    EventCategory,
    PersistenceFailed,
)
from boba.agent.state import ChannelId, StateChannel
from boba.llm.models import RequestId
from boba.workspace.contract import HistoryWorkspaceShell, WorkspaceError

__all__ = [
    "HistoryReader",
    "HistoryService",
    "HistoryStoreError",
    "HistoryStoreReadError",
    "HistoryStoreWriteError",
    "HistoryWriter",
    "InMemoryHistoryService",
    "JsonLinesHistoryService",
]


class HistoryStoreError(TerminalError[RequestId, AgentEvent]):
    """Базовая ошибка persistent-реализации HistoryService."""

    def __init__(self, reason: Exception, ctx: str = "") -> None:
        self.reason = reason
        self.ctx = ctx
        prefix = self._prefix()
        msg = f"{prefix}: {reason}"
        if ctx:
            msg = f"{prefix} ({ctx}): {reason}"
        super().__init__(msg)

    def _prefix(self) -> str:
        return "History store error"

    def to_user_feedback(self, request_id: RequestId) -> PersistenceFailed:
        return PersistenceFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
        )


class HistoryStoreWriteError(HistoryStoreError):
    """Не удалось записать событие в журнал."""

    def _prefix(self) -> str:
        return "Cannot write history event"


class HistoryStoreReadError(HistoryStoreError):
    """Не удалось прочитать журнал событий."""

    def _prefix(self) -> str:
        return "Cannot read history"


class HistoryReader(ABC):
    """Read-side порт журнала событий агента."""

    @abstractmethod
    def events(self) -> Iterator[AgentEvent]:
        """Все записанные события в порядке регистрации."""
        ...


class HistoryWriter(ABC):
    """Write-side порт журнала событий агента."""

    @abstractmethod
    def record(self, event: AgentEvent) -> None:
        """Зарегистрировать событие в журнале (с применением общего фильтра)."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Очистить журнал."""
        ...


class HistoryService(HistoryReader, HistoryWriter, StateChannel, ABC):
    """
    Композиция HistoryReader + HistoryWriter + StateChannel
    """

    @classmethod
    def channel_id(cls) -> ChannelId[Self]:
        return ChannelId("history")

    def record(self, event: AgentEvent) -> None:
        """Фильтрует ContentDeltaEvent и DiagnosticEvent сообщения.

        ContentDelta - инкрементальные чанки, журнал хранит только агрегаты.
        Diagnostic - эфемерная телеметрия, в журнал не идёт по дизайну.
        """
        if event.category in (
            EventCategory.CONTENT_DELTA,
            EventCategory.DIAGNOSTIC,
        ):
            return

        self._persist(event)

    @abstractmethod
    def _persist(self, event: AgentEvent) -> None:
        """Сохранить уже отфильтрованное событие в конкретный транспорт."""
        ...


class InMemoryHistoryService(HistoryService):
    """In-memory реализация HistoryService."""

    def __init__(self) -> None:
        self._events: list[AgentEvent] = []

    def _persist(self, event: AgentEvent) -> None:
        self._events.append(event)

    def events(self) -> Iterator[AgentEvent]:
        return iter(self._events)

    def clear(self) -> None:
        self._events.clear()


class JsonLinesHistoryService(HistoryService):
    """
    Journaling-реализация HistoryService поверх workspace

    Записывает все завершённые события:
        PhaseEvent / ContentSnapshotEvent / AdvisoryEvent / TerminalEvent

    Чанки (ContentDeltaEvent) пропускаются — журнал хранит
    только агрегированные снапшоты и переходы фаз
    """

    _FILENAME = "history.jsonl"

    def __init__(self, workspace: HistoryWorkspaceShell) -> None:
        self._workspace = workspace
        self._filename = self._FILENAME
        self._ensure_file()

    def _ensure_file(self) -> None:
        try:
            if self._workspace.exists(self._filename):
                return
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise HistoryStoreWriteError(exc, ctx=f"path={self._filename}") from exc

    def _persist(self, event: AgentEvent) -> None:
        line = AgentEventAdapter.dump_json(event).decode("utf-8")
        try:
            with self._workspace.append_text(self._filename) as f:
                f.write(line)
                f.write("\n")
        except WorkspaceError as exc:
            raise HistoryStoreWriteError(exc, ctx=f"path={self._filename}") from exc

    def events(self) -> Iterator[AgentEvent]:
        try:
            for line in self._workspace.read_lines(self._filename):
                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    yield AgentEventAdapter.validate_json(stripped)
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise HistoryStoreReadError(
                        exc, ctx=f"path={self._filename}: {stripped!r}",
                    ) from exc
        except WorkspaceError as exc:
            raise HistoryStoreReadError(exc, ctx=f"path={self._filename}") from exc

    def clear(self) -> None:
        try:
            with self._workspace.write_text(self._filename):
                pass
        except WorkspaceError as exc:
            raise HistoryStoreWriteError(exc, ctx=f"path={self._filename}") from exc
