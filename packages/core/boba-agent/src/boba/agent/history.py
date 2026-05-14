"""Порт журнала AgentEvent (Reader/Writer split) + реализации (in-memory, JSONL)."""

from __future__ import annotations

import dataclasses
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from enum import Enum
from typing import Any, Self

from boba.agent.errors import TerminalError
from boba.agent.event_specs import IsContentDelta
from boba.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    FeedbackToLLMAdded,
    GenerationDone,
    GenerationFailed,
    GenerationRetried,
    GenerationStarted,
    InvalidToolCallReceived,
    IterationStarted,
    LLMRequestSent,
    LLMResponseStreamOpened,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    ThinkingComplete,
    ThinkingStarted,
    ToolCallComplete,
    ToolCallStreamStarted,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserQueryReceived,
)
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.agent.state import ChannelId, StateChannel
from boba.llm.events import FinishReason
from boba.llm.models import InvalidToolCall, RequestId, ToolCall
from boba.patterns import Id
from boba.tools.domain import (
    ErrorResult,
    JsonResult,
    TextResult,
    ToolResult,
)
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


_SKIP_RECORD: IsContentDelta = IsContentDelta()


class HistoryService(HistoryReader, HistoryWriter, StateChannel, ABC):
    """Композиция HistoryReader + HistoryWriter + StateChannel.

    Шлюз `record()` применяет общий фильтр (`IsContentDelta` инвертом) ко всем
    реализациям — chunk-события (ContentDelta) в журнал не попадают независимо
    от транспорта. Наследники реализуют только `_persist(event)`.
    """

    @classmethod
    def channel_id(cls) -> ChannelId[Self]:
        return ChannelId("history")

    def record(self, event: AgentEvent) -> None:
        if _SKIP_RECORD.check(event):
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
        PhaseTransition / ContentSnapshot / Advisory / Terminal

    Чанки (ContentDelta) пропускаются — журнал хранит
    только агрегированные снапшоты и переходы фаз
    """

    _DEFAULT_FILENAME = "history.jsonl"

    def __init__(
        self,
        workspace: HistoryWorkspaceShell,
        filename: str = _DEFAULT_FILENAME,
    ) -> None:
        self._workspace = workspace
        self._filename = filename
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
        line = self._encode(event)
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
                    yield self._decode(stripped)
                except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
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

    @staticmethod
    def _encode(event: AgentEvent) -> str:
        payload: dict[str, Any] = {
            "type": event.name(),
            **JsonLinesHistoryService._to_jsonable(event),
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _to_jsonable(value: Any) -> Any:  # noqa: PLR0911
        """Рекурсивно конвертирует dataclass-граф в JSON-совместимое значение.

        Знает три семейства нестандартных типов: `Id` (→ to_wire),
        `Enum` (→ value), `ToolResult` (sealed → kind-discriminated). Всё
        остальное разворачивается рефлексией dataclass'ов.
        """
        if value is None or isinstance(value, (str, int, bool, float)):
            return value
        if isinstance(value, Id):
            return value.to_wire()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, ToolResult):
            return JsonLinesHistoryService._encode_tool_result(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                f.name: JsonLinesHistoryService._to_jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value)
            }
        if isinstance(value, Mapping):
            return {
                k: JsonLinesHistoryService._to_jsonable(v) for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [JsonLinesHistoryService._to_jsonable(v) for v in value]
        kind = type(value).__name__
        msg = f"JsonLinesHistoryService: cannot encode {kind}"
        raise ValueError(msg)

    @staticmethod
    def _decode(line: str) -> AgentEvent:  # noqa: C901, PLR0911, PLR0912
        cls = JsonLinesHistoryService
        raw = json.loads(line)
        event_type = raw["type"]
        rid = RequestId.from_wire(raw["request_id"])
        match event_type:
            case "IterationStarted":
                return IterationStarted(
                    request_id=rid,
                    iteration=raw["iteration"],
                    max_iterations=raw["max_iterations"],
                )
            case "LLMRequestSent":
                return LLMRequestSent(
                    request_id=rid,
                    model=raw["model"],
                    messages_count=raw["messages_count"],
                    has_tools=raw["has_tools"],
                    monotonic_ns=raw["monotonic_ns"],
                )
            case "LLMResponseStreamOpened":
                return LLMResponseStreamOpened(
                    request_id=rid, monotonic_ns=raw["monotonic_ns"],
                )
            case "GenerationStarted":
                return GenerationStarted(request_id=rid)
            case "ThinkingStarted":
                return ThinkingStarted(request_id=rid)
            case "AnswerStarted":
                return AnswerStarted(request_id=rid)
            case "ToolCallStreamStarted":
                return ToolCallStreamStarted(
                    request_id=rid,
                    index=raw["index"],
                    tool_call_id=raw["tool_call_id"],
                    tool_name=raw["tool_name"],
                )
            case "ToolExecutionStarted":
                return ToolExecutionStarted(
                    request_id=rid, call=cls._decode_tool_call(raw["call"]),
                )
            case "GenerationRetried":
                return GenerationRetried(
                    request_id=rid,
                    attempt=raw["attempt"],
                    reason=raw["reason"],
                    status_code=raw.get("status_code"),
                )
            case "GenerationDone":
                return GenerationDone(
                    request_id=rid, finish_reason=FinishReason(raw["finish_reason"]),
                )
            case "UserQueryReceived":
                return UserQueryReceived(request_id=rid, query=raw["query"])
            case "ThinkingComplete":
                return ThinkingComplete(request_id=rid, content=raw["content"])
            case "AnswerComplete":
                return AnswerComplete(request_id=rid, content=raw["content"])
            case "RefusalComplete":
                return RefusalComplete(request_id=rid, content=raw["content"])
            case "ToolCallComplete":
                return ToolCallComplete(
                    request_id=rid, call=cls._decode_tool_call(raw["call"]),
                )
            case "ToolResultReady":
                return ToolResultReady(
                    request_id=rid,
                    call=cls._decode_tool_call(raw["call"]),
                    result=cls._decode_tool_call_result(raw["result"]),
                )
            case "FeedbackToLLMAdded":
                return FeedbackToLLMAdded(request_id=rid, content=raw["content"])
            case "ToolExecutionFailed":
                return ToolExecutionFailed(
                    request_id=rid,
                    call=cls._decode_tool_call(raw["call"]),
                    failure=cls._decode_tool_call_failure(raw["failure"]),
                )
            case "InvalidToolCallReceived":
                return InvalidToolCallReceived(
                    request_id=rid,
                    invalid=cls._decode_invalid_tool_call(raw["invalid"]),
                )
            case "GenerationFailed":
                return GenerationFailed(
                    request_id=rid,
                    error_kind=raw["error_kind"],
                    message=raw["message"],
                )
            case "PromptFailed":
                return PromptFailed(
                    request_id=rid,
                    error_kind=raw["error_kind"],
                    message=raw["message"],
                    provider=raw.get("provider"),
                )
            case "MaxIterationsReached":
                return MaxIterationsReached(
                    request_id=rid,
                    error_kind=raw["error_kind"],
                    message=raw["message"],
                    limit=raw["limit"],
                    iteration=raw["iteration"],
                )
            case "PersistenceFailed":
                return PersistenceFailed(
                    request_id=rid,
                    error_kind=raw["error_kind"],
                    message=raw["message"],
                )
            case _:
                msg = f"JsonLinesHistoryService: неизвестный type='{event_type}'"
                raise ValueError(msg)

    @staticmethod
    def _decode_tool_call(raw: Mapping[str, Any]) -> ToolCall:
        return ToolCall(id=raw["id"], name=raw["name"], args=raw["args"])

    @staticmethod
    def _decode_invalid_tool_call(raw: Mapping[str, Any]) -> InvalidToolCall:
        return InvalidToolCall(
            id=raw["id"],
            name=raw["name"],
            raw_args=raw["raw_args"],
            error=raw["error"],
        )

    @staticmethod
    def _decode_tool_call_failure(raw: Mapping[str, Any]) -> ToolCallFailure:
        return ToolCallFailure(error_kind=raw["error_kind"], message=raw["message"])

    @staticmethod
    def _decode_tool_call_result(raw: Mapping[str, Any]) -> ToolCallResult:
        return ToolCallResult(
            result=JsonLinesHistoryService._decode_tool_result(raw["result"]),
        )

    @staticmethod
    def _encode_tool_result(result: ToolResult) -> dict[str, Any]:
        match result:
            case TextResult(text=text, metadata=metadata):
                return {"kind": "text", "text": text, "metadata": dict(metadata)}
            case JsonResult(payload=payload, metadata=metadata):
                return {
                    "kind": "json",
                    "payload": payload,
                    "metadata": dict(metadata),
                }
            case ErrorResult(
                message=message, error_kind=error_kind, metadata=metadata,
            ):
                return {
                    "kind": "error",
                    "message": message,
                    "error_kind": error_kind,
                    "metadata": dict(metadata),
                }
            case _:
                kind = type(result).__name__
                msg = f"JsonLinesHistoryService: неизвестный ToolResult: {kind}"
                raise ValueError(msg)

    @staticmethod
    def _decode_tool_result(raw: Mapping[str, Any]) -> ToolResult:
        kind = raw["kind"]
        metadata = raw.get("metadata", {})
        match kind:
            case "text":
                return TextResult(text=raw["text"], metadata=metadata)
            case "json":
                return JsonResult(payload=raw["payload"], metadata=metadata)
            case "error":
                return ErrorResult(
                    message=raw["message"],
                    error_kind=raw["error_kind"],
                    metadata=metadata,
                )
            case _:
                msg = f"JsonLinesHistoryService: неизвестный result.kind='{kind}'"
                raise ValueError(msg)
