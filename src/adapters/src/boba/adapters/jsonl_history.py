"""JsonLines реализация HistoryService + JSON-сериализация HistoryEntry."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import get_args
from uuid import UUID

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import RequestId
from boba.domain.core.history import (
    EntryId,
    HistoryEntry,
    HistoryService,
    HistoryWriteError,
)
from boba.domain.core.patterns import Converter
from boba.domain.core.workspace import WorkspaceService

_EVENT_TYPES: dict[str, type] = {
    cls.__name__: cls for cls in get_args(AgentEvent)
}


def _serialize_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, RequestId):
        return str(value.name)
    return value


class HistoryEntryEncoder(Converter[HistoryEntry, str]):
    """HistoryEntry → JSON line."""

    def convert(self, entry: HistoryEntry) -> str:
        event_data = asdict(entry.event)
        return json.dumps(
            {
                "id": str(entry.id.name),
                "parent_id": str(entry.parent_id.name) if entry.parent_id else None,
                "request_id": str(entry.request_id.name),
                "timestamp": entry.timestamp.isoformat(),
                "event": {
                    "event_type": type(entry.event).__name__,
                    "event_data": {
                        k: _serialize_value(v) for k, v in event_data.items()
                    },
                },
            },
            ensure_ascii=False,
        )


class HistoryEntryDecoder(Converter[str, HistoryEntry]):
    """JSON line → HistoryEntry."""

    def convert(self, value: str) -> HistoryEntry:
        raw = json.loads(value)
        event_raw = raw["event"]

        cls = _EVENT_TYPES.get(event_raw["event_type"])
        if cls is None:
            raise ValueError(f"Unknown event type: {event_raw['event_type']}")

        event_data = event_raw["event_data"]
        if "request_id" in event_data and isinstance(event_data["request_id"], str):
            event_data = {
                **event_data,
                "request_id": RequestId.from_uuid(event_data["request_id"]),
            }

        event: AgentEvent = cls(**event_data)
        sdf = raw["id"]
        return HistoryEntry(
            id=EntryId.from_uuid(sdf),
            parent_id=EntryId.from_uuid(raw["parent_id"]) if raw["parent_id"] else None,
            request_id=RequestId.from_uuid(raw["request_id"]),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            event=event,
        )


class JsonLinesHistoryService(HistoryService):
    """Журнал истории в формате jsonlines. Один файл на workspace."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace
        self._encoder = HistoryEntryEncoder()
        self._decoder = HistoryEntryDecoder()
        self._last_id: EntryId | None = None
        self._history_file = "history.jsonl"

        self._ensure_file()
        self._recover_last_id()

    def _ensure_file(self) -> None:
        if self._workspace.exists(self._history_file):
            return
        try:
            with self._workspace.write_text(self._history_file):
                pass
        except OSError as exc:
            raise HistoryWriteError(exc, ctx=f"path={self._history_file}") from exc

    def _recover_last_id(self) -> None:
        for line in self._workspace.read_lines(self._history_file, reverse=True):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                self._last_id = self._decoder.convert(stripped).id
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            return

    def append(self, event: AgentEvent) -> HistoryEntry:
        entry = HistoryEntry(
            id=EntryId.new(),
            parent_id=self._last_id,
            request_id=event.request_id,
            timestamp=datetime.now(UTC),
            event=event,
        )

        line = self._encoder.convert(entry)

        try:
            with self._workspace.append_text(self._history_file) as f:
                f.write(line)
                f.write("\n")
        except OSError as exc:
            raise HistoryWriteError(exc, ctx=f"path={self._history_file}") from exc

        self._last_id = entry.id
        return entry

    def entries(self) -> Iterator[HistoryEntry]:
        for line in self._workspace.read_lines(self._history_file):
            if line.strip():
                yield self._decoder.convert(line)
