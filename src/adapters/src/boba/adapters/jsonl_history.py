"""JsonLines реализация HistoryService + JSON-сериализация HistoryEntry."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import assert_never

from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
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
    HistoryWriteError,
)
from boba.domain.core.patterns import Converter
from boba.domain.core.workspace import WorkspaceService


class HistoryEntryEncoder(Converter[HistoryEntry, str]):
    """HistoryEntry → JSON line."""

    def convert(self, entry: HistoryEntry) -> str:
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

    def _encode_event(  # noqa: C901, PLR0911, PLR0912
        self, event: AgentEvent
    ) -> dict[str, object]:
        match event:
            case UserQueryReceived(request_id=rid, query=q):
                return {"request_id": rid.to_wire(), "query": q}
            case StageStarted(request_id=rid, stage=s):
                return {"request_id": rid.to_wire(), "stage": s}
            case StageCompleted(request_id=rid, stage=s, detail=d):
                return {"request_id": rid.to_wire(), "stage": s, "detail": d}
            case GenerationStarted(request_id=rid):
                return {"request_id": rid.to_wire()}
            case ThinkingStarted(request_id=rid):
                return {"request_id": rid.to_wire()}
            case ThinkingToken(request_id=rid, token=t):
                return {"request_id": rid.to_wire(), "token": t}
            case ThinkingComplete(request_id=rid, content=c):
                return {"request_id": rid.to_wire(), "content": c}
            case AnswerStarted(request_id=rid):
                return {"request_id": rid.to_wire()}
            case AnswerToken(request_id=rid, token=t):
                return {"request_id": rid.to_wire(), "token": t}
            case AnswerComplete(request_id=rid, content=c):
                return {"request_id": rid.to_wire(), "content": c}
            case RefusalToken(request_id=rid, token=t):
                return {"request_id": rid.to_wire(), "token": t}
            case RefusalComplete(request_id=rid, content=c):
                return {"request_id": rid.to_wire(), "content": c}
            case GenerationDone(request_id=rid, finish_reason=fr):
                return {"request_id": rid.to_wire(), "finish_reason": fr}
            case ToolCallBegin(
                request_id=rid,
                index=idx,
                tool_call_id=tid,
                tool_name=tn,
            ):
                return {
                    "request_id": rid.to_wire(),
                    "index": idx,
                    "tool_call_id": tid,
                    "tool_name": tn,
                }
            case ToolCallArgumentDelta(request_id=rid, index=idx, arguments=a):
                return {
                    "request_id": rid.to_wire(),
                    "index": idx,
                    "arguments": a,
                }
            case ToolCallComplete(
                request_id=rid, tool_call_id=tid, tool_name=tn, arguments=a
            ):
                return {
                    "request_id": rid.to_wire(),
                    "tool_call_id": tid,
                    "tool_name": tn,
                    "arguments": a,
                }
            case ToolResultReady(
                request_id=rid,
                tool_call_id=tid,
                tool_name=tn,
                content=c,
                is_error=e,
            ):
                return {
                    "request_id": rid.to_wire(),
                    "tool_call_id": tid,
                    "tool_name": tn,
                    "content": c,
                    "is_error": e,
                }
            case _ as unreachable:
                assert_never(unreachable)


class HistoryEntryDecoder(Converter[str, HistoryEntry]):
    """JSON line → HistoryEntry."""

    def convert(self, value: str) -> HistoryEntry:
        raw = json.loads(value)
        event = self._decode_event(raw["event_type"], raw["event"])

        return HistoryEntry(
            id=EntryId.from_wire(raw["id"]),
            parent_id=(
                EntryId.from_wire(raw["parent_id"]) if raw["parent_id"] else None
            ),
            request_id=RequestId.from_wire(raw["request_id"]),
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            event=event,
        )

    def _decode_event(  # noqa: C901, PLR0911, PLR0912
        self, event_type: str, data: dict[str, object]
    ) -> AgentEvent:
        rid = RequestId.from_wire(str(data["request_id"]))
        match event_type:
            case "UserQueryReceived":
                return UserQueryReceived(request_id=rid, query=str(data["query"]))
            case "StageStarted":
                return StageStarted(request_id=rid, stage=str(data["stage"]))
            case "StageCompleted":
                return StageCompleted(
                    request_id=rid,
                    stage=str(data["stage"]),
                    detail=str(data["detail"]),
                )
            case "GenerationStarted":
                return GenerationStarted(request_id=rid)
            case "ThinkingStarted":
                return ThinkingStarted(request_id=rid)
            case "ThinkingToken":
                return ThinkingToken(request_id=rid, token=str(data["token"]))
            case "ThinkingComplete":
                return ThinkingComplete(request_id=rid, content=str(data["content"]))
            case "AnswerStarted":
                return AnswerStarted(request_id=rid)
            case "AnswerToken":
                return AnswerToken(request_id=rid, token=str(data["token"]))
            case "AnswerComplete":
                return AnswerComplete(request_id=rid, content=str(data["content"]))
            case "RefusalToken":
                return RefusalToken(request_id=rid, token=str(data["token"]))
            case "RefusalComplete":
                return RefusalComplete(request_id=rid, content=str(data["content"]))
            case "GenerationDone":
                return GenerationDone(
                    request_id=rid, finish_reason=str(data["finish_reason"])
                )
            case "ToolCallBegin":
                return ToolCallBegin(
                    request_id=rid,
                    index=int(data["index"]),  # type: ignore[arg-type]
                    tool_call_id=str(data["tool_call_id"]),
                    tool_name=str(data["tool_name"]),
                )
            case "ToolCallArgumentDelta":
                return ToolCallArgumentDelta(
                    request_id=rid,
                    index=int(data["index"]),  # type: ignore[arg-type]
                    arguments=str(data["arguments"]),
                )
            case "ToolCallComplete":
                return ToolCallComplete(
                    request_id=rid,
                    tool_call_id=str(data["tool_call_id"]),
                    tool_name=str(data["tool_name"]),
                    arguments=str(data["arguments"]),
                )
            case "ToolResultReady":
                return ToolResultReady(
                    request_id=rid,
                    tool_call_id=str(data["tool_call_id"]),
                    tool_name=str(data["tool_name"]),
                    content=str(data["content"]),
                    is_error=bool(data["is_error"]),
                )
            case _ as unreachable:
                assert_never(unreachable)


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

    def entries(self, *, reverse: bool = False) -> Iterator[HistoryEntry]:
        """Итерация по истории.

        reverse=True — от последней записи к первой (быстрое чтение «хвоста»
        истории чата, не загружая весь файл в память линейно).
        Незавершённые и повреждённые JSON-строки пропускаются.
        """
        for line in self._workspace.read_lines(self._history_file, reverse=reverse):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield self._decoder.convert(stripped)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
