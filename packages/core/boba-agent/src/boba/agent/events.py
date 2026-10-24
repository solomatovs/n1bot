"""Порт наружу: события хода, которые агент отдаёт запускающей программе.

Виды событий: первое событие сеанса, кусок ответа с идентификатором
сообщения, записанная запись разговора, сброс записей в хранилище истории,
начало исполнения инструмента,
отчёт сервера о ходе вызова, итог хода с элементами очереди, которые он
обслужил, ход сжатия разговора, пометка пользователю посреди хода, ошибка
вне хода, вопрос наружу и его снятие. Как события уходят
на провод — дело реализации порта (вход процесса); ядро только зовёт `emit`.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.agent.control import CanUseToolRequest
from boba.agent.records import PermissionMode, Record, Usage

__all__ = [
    "AgentEvent",
    "CompactionEvent",
    "CompactionStatus",
    "ControlCancelEvent",
    "ControlRequestEvent",
    "ErrorEvent",
    "EventSink",
    "HistoryWrittenEvent",
    "InitEvent",
    "NoticeEvent",
    "RecordEvent",
    "ResultEvent",
    "StreamEvent",
    "ToolProgressEvent",
    "ToolStartedEvent",
    "TurnOutcome",
]


class TurnOutcome(StrEnum):
    """Исходы хода — закрытый перечень раздела 5.13 плана."""

    COMPLETED = "completed"
    ABORTED_STREAMING = "aborted_streaming"
    ABORTED_TOOLS = "aborted_tools"
    API_ERROR = "api_error"
    BLOCKING_LIMIT = "blocking_limit"
    RAPID_REFILL_BREAKER = "rapid_refill_breaker"
    COMPACT_FAILURE_BREAKER = "compact_failure_breaker"
    MAX_TURNS = "max_turns"
    OUTPUT_LIMIT = "output_limit"
    SHUTDOWN = "shutdown"

    @property
    def is_failure(self) -> bool:
        """Ход кончился отказом модели: ответа нет, текст причины — в итоге."""
        return self in (TurnOutcome.API_ERROR, TurnOutcome.OUTPUT_LIMIT)

    @property
    def has_reason(self) -> bool:
        """Ход кончился с причиной, которую агент записал текстом: отказ
        модели, запрос больше окна, срабатывание предохранителей сжатия."""
        if self.is_failure:
            return True

        return self in (
            TurnOutcome.BLOCKING_LIMIT,
            TurnOutcome.RAPID_REFILL_BREAKER,
            TurnOutcome.COMPACT_FAILURE_BREAKER,
        )


class EventModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class InitEvent(EventModel):
    """Первое событие сеанса."""

    session_id: str
    resumed: bool
    model: str
    tools: Sequence[str]
    permission_mode: PermissionMode
    version: str


class StreamEvent(EventModel):
    """Кусок ответа модели по мере прихода; `message_id` — идентификатор
    сообщения ассистента, под которым ответ будет записан: он выдан до
    первого куска, чтобы приёмник группировал куски по сообщению."""

    content: str = ""
    reasoning: str = ""
    message_id: str = ""


class ToolStartedEvent(EventModel):
    """Вызов инструмента ушёл на исполнение: разрешение получено, сервер
    или собственный инструмент начал работу."""

    tool_use_id: str


class ToolProgressEvent(EventModel):
    """Отчёт сервера о ходе вызова: сделано `progress` из `total` (None —
    итог неизвестен), `message` — что происходит, `elapsed_sec` — время с
    начала вызова, `meta` — `_meta` уведомления сервера как есть (пусто —
    сервер его не прислал)."""

    tool_use_id: str
    message: str = ""
    progress: float
    total: float | None = None
    elapsed_sec: float
    meta: Mapping[str, JsonValue]


class RecordEvent(EventModel):
    """Записана запись разговора."""

    record: Record


class HistoryWrittenEvent(EventModel):
    """Записи сессии сохранены в хранилище истории; `recorded_at` — время
    последней из них строкой журнала. Шлёт писатель после каждого сброса."""

    recorded_at: str = Field(min_length=1)


class ResultEvent(EventModel):
    """Конец хода: исход, учёт и идентификаторы элементов очереди, которые
    ход обслужил; `error` — причина конца хода, когда она есть
    (TurnOutcome.has_reason)."""

    outcome: TurnOutcome
    session_id: str
    usage: Usage
    num_turns: int
    duration_ms: int
    prompt_ids: Sequence[str] = ()
    error: str = ""


class CompactionStatus(StrEnum):
    """Стадии сжатия разговора — значения status уведомления
    compaction_update ACP."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompactionEvent(EventModel):
    """Сжатие разговора: начато, состоялось с пересказом, не удалось с
    причиной или прервано. `compaction_id` — uuid записи пересказа: по нему
    воспроизведение истории выдаёт то же сжатие."""

    compaction_id: str
    status: CompactionStatus
    summary: str = ""
    error: str = ""


class NoticeEvent(EventModel):
    """Пометка пользователю посреди хода: агент сообщает о том, что требует
    его действия (тред больше не помещается в окно модели)."""

    title: str
    description: str


class ErrorEvent(EventModel):
    """Ошибка вне хода: строка ввода не разобрана, сброс журнала не удался."""

    message: str
    unflushed: int = 0


class ControlRequestEvent(EventModel):
    """Вопрос агента наружу: `control_request` с `can_use_tool`. Ответ
    приходит через `Agent.answer` с тем же `request_id`."""

    request_id: str
    request: CanUseToolRequest


class ControlCancelEvent(EventModel):
    """Вопрос снят отменой хода: `control_cancel_request`; поздний ответ
    отбрасывается."""

    request_id: str


AgentEvent: TypeAlias = (
    InitEvent
    | StreamEvent
    | RecordEvent
    | HistoryWrittenEvent
    | ToolStartedEvent
    | ToolProgressEvent
    | ResultEvent
    | CompactionEvent
    | NoticeEvent
    | ErrorEvent
    | ControlRequestEvent
    | ControlCancelEvent
)


class EventSink(Protocol):
    """Приёмник событий агента."""

    @abstractmethod
    async def emit(self, event: AgentEvent) -> None: ...
