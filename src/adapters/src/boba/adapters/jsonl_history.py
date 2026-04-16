"""JsonLines реализация HistoryService + JSON-сериализация AgentEvent."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    EventSerializer,
    GenerationDone,
    GenerationStarted,
    RefusalComplete,
    RefusalToken,
    StageCompleted,
    StageStarted,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.models import RequestId
from boba.domain.core.history import (
    EntryId,
    HistoryEntry,
    HistoryService,
)
from boba.domain.core.patterns import Converter
from boba.domain.core.workspace import WorkspaceService

_EVENT_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        UserQueryReceived,
        StageStarted,
        StageCompleted,
        GenerationStarted,
        ThinkingStarted,
        ThinkingToken,
        ThinkingComplete,
        AnswerStarted,
        AnswerToken,
        AnswerComplete,
        RefusalToken,
        RefusalComplete,
        GenerationDone,
        ToolCallBegin,
        ToolCallArgumentDelta,
        ToolCallComplete,
        ToolResultReady,
    )
}


def _serialize_value(value: object) -> object:
    """Сериализация значений, не поддерживаемых JSON."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, RequestId):
        return str(value.name)
    return value


class AgentEventEncoder(Converter[AgentEvent, str]):
    """AgentEvent → JSON string."""

    def convert(self, value: AgentEvent) -> str:
        data = asdict(value)
        return json.dumps(
            {
                "event_type": type(value).__name__,
                "event_data": {k: _serialize_value(v) for k, v in data.items()},
            },
            ensure_ascii=False,
        )


class AgentEventDecoder(Converter[str, AgentEvent]):
    """JSON string → AgentEvent."""

    def convert(self, value: str) -> AgentEvent:
        raw = json.loads(value)
        event_type = raw["event_type"]
        event_data = raw["event_data"]
        cls = _EVENT_TYPES.get(event_type)
        if cls is None:
            raise ValueError(f"Unknown event type: {event_type}")
        if "request_id" in event_data and isinstance(event_data["request_id"], str):
            event_data = {
                **event_data,
                "request_id": RequestId(UUID(event_data["request_id"])),
            }
        return cls(**event_data)


class JsonLinesHistoryService(HistoryService):
    """Журнал истории в формате jsonlines. Один файл на workspace."""

    def __init__(
        self, workspace: WorkspaceService, serializer: EventSerializer
    ) -> None:
        self._workspace = workspace
        self._serializer = serializer
        self._last_id: EntryId | None = None
        self.HISTORY_FILE = "history.jsonl"

        # восстановить last_id из существующего файла
        if self._workspace.exists(self.HISTORY_FILE):
            for entry in self.entries():
                self._last_id = entry.id

    def append(self, event: AgentEvent) -> HistoryEntry:
        event_json = self._serializer.serialize(event)

        entry_id = EntryId(uuid4())
        now = datetime.now(UTC)

        entry = HistoryEntry(
            id=entry_id,
            parent_id=self._last_id,
            request_id=event.request_id,
            timestamp=now,
            event=event,
        )

        line = json.dumps(
            {
                "id": str(entry_id.name),
                "parent_id": str(self._last_id.name) if self._last_id else None,
                "request_id": str(event.request_id.name),
                "timestamp": now.isoformat(),
                "event": json.loads(event_json),
            },
            ensure_ascii=False,
        )

        with self._workspace.append_text(self.HISTORY_FILE) as f:
            f.write(line + "\n")

        self._last_id = entry_id
        return entry

    def entries(self) -> Iterator[HistoryEntry]:
        if not self._workspace.exists(self.HISTORY_FILE):
            return

        with self._workspace.read_text(self.HISTORY_FILE) as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if stripped:
                    yield self._from_dict(json.loads(stripped))

    def entries_by_request(self, request_id: RequestId) -> Iterator[HistoryEntry]:
        for entry in self.entries():
            if entry.request_id == request_id:
                yield entry

    def requests(self) -> Iterator[RequestId]:
        seen: set[RequestId] = set()
        for entry in self.entries():
            if entry.request_id not in seen:
                seen.add(entry.request_id)
                yield entry.request_id

    def _from_dict(self, raw: dict) -> HistoryEntry:
        event = self._serializer.deserialize(
            json.dumps(raw["event"], ensure_ascii=False)
        )

        return HistoryEntry(
            id=EntryId(UUID(raw["id"])),
            parent_id=EntryId(UUID(raw["parent_id"])) if raw["parent_id"] else None,
            request_id=RequestId(UUID(raw["request_id"])),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            event=event,
        )
