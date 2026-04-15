"""JsonLines реализация HistoryService через WorkspaceService."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterator
from uuid import UUID, uuid4

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.serialization import EventSerializer
from boba.domain.core.history import (
    EntryId,
    HistoryEntry,
    HistoryService,
    RequestId,
)
from boba.domain.core.workspace import WorkspaceService


class JsonLinesHistoryService(HistoryService):
    """Журнал истории в формате jsonlines. Один файл на workspace."""

    def __init__(
        self, workspace: WorkspaceService, serializer: EventSerializer
    ) -> None:
        self._workspace = workspace
        self._serializer = serializer
        self._last_id: EntryId | None = None
        self.HISTORY_FILE = "history.jsonl"

        # восстановить _last_id из существующего файла
        if self._workspace.exists(self.HISTORY_FILE):
            for entry in self.entries():
                self._last_id = entry.id

    def append(self, request_id: RequestId, event: AgentEvent) -> HistoryEntry:
        event_type, event_data = self._serializer.serialize(event)

        entry_id = EntryId(uuid4())
        now = datetime.now(timezone.utc)

        entry = HistoryEntry(
            id=entry_id,
            parent_id=self._last_id,
            request_id=request_id,
            timestamp=now,
            event=event,
        )

        line = json.dumps(
            {
                "id": str(entry_id.name),
                "parent_id": str(self._last_id.name) if self._last_id else None,
                "request_id": str(request_id.name),
                "timestamp": now.isoformat(),
                "event_type": event_type,
                "event_data": event_data,
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
            for line in f:
                line = line.strip()
                if line:
                    yield self._from_dict(json.loads(line))

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
        event = self._serializer.deserialize((raw["event_type"], raw["event_data"]))

        return HistoryEntry(
            id=EntryId(UUID(raw["id"])),
            parent_id=EntryId(UUID(raw["parent_id"])) if raw["parent_id"] else None,
            request_id=RequestId(UUID(raw["request_id"])),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            event=event,
        )
