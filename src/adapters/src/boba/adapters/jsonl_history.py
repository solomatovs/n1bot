"""JsonLines реализация HistoryService + JSON-сериализация HistoryEntry."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import MISSING, fields
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, ClassVar, cast, get_args

from boba.domain.agent.events import AgentEvent, AgentEventName, BaseEvent
from boba.domain.agent.history import (
    EntryId,
    HistoryEntry,
    HistoryReadError,
    HistoryService,
    HistoryWriteError,
)
from boba.domain.agent.models import RequestId
from boba.domain.core.patterns import (
    Converter,
    ConverterError,
    ConverterInputError,
    ConverterOutputError,
    UuId,
)
from boba.domain.core.workspace import SystemWorkspaceService, WorkspaceError


class HistoryEntryEncoder(Converter[HistoryEntry, str]):
    """HistoryEntry → JSON line."""

    def convert(self, entry: HistoryEntry) -> str:
        try:
            return self._convert(entry)
        except ConverterError:
            raise
        except (TypeError, ValueError) as e:
            raise ConverterOutputError(f"failed to encode HistoryEntry: {e}") from e

    def _convert(self, entry: HistoryEntry) -> str:
        return json.dumps(
            {
                "id": entry.id.to_wire(),
                "parent_id": (entry.parent_id.to_wire() if entry.parent_id else None),
                "request_id": entry.request_id.to_wire(),
                "timestamp": entry.timestamp.isoformat(),
                "event_type": entry.event.name(),
                "event": self._encode_event(entry.event),
            },
            ensure_ascii=False,
        )

    def _encode_event(self, event: AgentEvent) -> dict[str, object]:
        out: dict[str, object] = {}
        for f in fields(event):
            val = getattr(event, f.name)
            if isinstance(val, UuId):
                val = val.to_wire()
            out[f.name] = val
        return out


class HistoryEntryDecoder(Converter[str, HistoryEntry]):
    """JSON line → HistoryEntry."""

    _EVENT_CLASSES: ClassVar[Mapping[str, type[BaseEvent]]] = MappingProxyType(
        {cls.name(): cls for cls in get_args(AgentEvent)}
    )
    _KNOWN_EVENT_NAMES: frozenset[str] = frozenset(get_args(AgentEventName))

    def convert(self, value: str) -> HistoryEntry:
        try:
            return self._convert(value)
        except ConverterInputError as e:
            raise ConverterInputError(f"{e} | input={value!r}") from e.__cause__
        except ConverterError:
            raise
        except json.JSONDecodeError as e:
            raise ConverterInputError(
                f"malformed JSON: {e.msg} | input={value!r}"
            ) from e
        except KeyError as e:
            raise ConverterInputError(
                f"missing required field: {e.args[0]!r} | input={value!r}"
            ) from e
        except (ValueError, TypeError) as e:
            raise ConverterInputError(f"{e} | input={value!r}") from e

    def _convert(self, value: str) -> HistoryEntry:
        raw = json.loads(value)

        event_type_raw = raw["event_type"]
        if event_type_raw not in self._KNOWN_EVENT_NAMES:
            raise ConverterInputError(f"unknown event_type: {event_type_raw!r}")

        event_type = cast(AgentEventName, event_type_raw)

        event = self._decode_event(event_type, raw["event"])

        return HistoryEntry(
            id=EntryId.from_wire(raw["id"]),
            parent_id=(
                EntryId.from_wire(raw["parent_id"]) if raw["parent_id"] else None
            ),
            request_id=RequestId.from_wire(raw["request_id"]),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            event=event,
        )

    def _decode_event(
        self, event_type: AgentEventName, data: dict[str, object]
    ) -> AgentEvent:
        cls = self._EVENT_CLASSES[event_type]
        for f in fields(cls):
            if (
                f.default is MISSING
                and f.default_factory is MISSING
                and f.name not in data
            ):
                raise KeyError(f.name)
        kwargs: dict[str, Any] = dict(data)
        kwargs["request_id"] = RequestId.from_wire(str(data["request_id"]))
        return cast(AgentEvent, cls(**kwargs))


class JsonLinesHistoryService(HistoryService):
    """Журнал истории в формате jsonlines. Один файл на workspace."""

    def __init__(self, workspace: SystemWorkspaceService) -> None:
        self._workspace = workspace
        self._encoder = HistoryEntryEncoder()
        self._decoder = HistoryEntryDecoder()
        self._last_id: EntryId | None = None
        self._history_file = "history.jsonl"

        self._ensure_file()
        self._recover_last_id()

    def _ensure_file(self) -> None:
        try:
            if self._workspace.exists(self._history_file):
                return
            with self._workspace.write_text(self._history_file):
                pass
        except WorkspaceError as exc:
            raise HistoryWriteError(exc, ctx=f"path={self._history_file}") from exc

    def _recover_last_id(self) -> None:
        try:
            for line in self._workspace.read_lines(self._history_file, reverse=True):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    self._last_id = self._decoder.convert(stripped).id
                except ConverterError:
                    continue
                return
        except WorkspaceError as exc:
            raise HistoryReadError(exc, ctx=f"path={self._history_file}") from exc

    def append(self, event: AgentEvent) -> HistoryEntry:
        entry = HistoryEntry(
            id=EntryId.new(),
            parent_id=self._last_id,
            request_id=event.request_id,
            timestamp=datetime.now(UTC),
            event=event,
        )

        try:
            line = self._encoder.convert(entry)
        except ConverterError as exc:
            raise HistoryWriteError(exc, ctx=f"path={self._history_file}") from exc

        try:
            with self._workspace.append_text(self._history_file) as f:
                f.write(line)
                f.write("\n")
        except WorkspaceError as exc:
            raise HistoryWriteError(exc, ctx=f"path={self._history_file}") from exc

        self._last_id = entry.id
        return entry

    def entries(self, *, reverse: bool = False) -> Iterator[HistoryEntry]:
        """Итерация по истории.

        reverse=True — от последней записи к первой (быстрое чтение «хвоста»
        истории чата, не загружая весь файл в память линейно).
        Пустые строки пропускаются.

        Raises:
            HistoryReadError: ошибка доступа к хранилищу либо битая/усечённая
                JSON-строка в журнале.
        """
        try:
            for line in self._workspace.read_lines(self._history_file, reverse=reverse):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield self._decoder.convert(stripped)
                except ConverterError as exc:
                    raise HistoryReadError(
                        exc, ctx=f"path={self._history_file}: {stripped}"
                    ) from exc
        except WorkspaceError as exc:
            raise HistoryReadError(exc, ctx=f"path={self._history_file}") from exc
