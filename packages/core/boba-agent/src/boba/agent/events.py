"""Порт наружу: события хода, которые агент отдаёт запускающей программе.

Виды событий: первое событие сеанса, кусок ответа с идентификатором
сообщения, записанная запись разговора, сброс записей в хранилище истории,
начало исполнения инструмента, отчёт сервера о ходе вызова, конец хода его
записью turn_end, ошибка вне хода, вопрос наружу и его снятие. Как события
уходят на провод — дело реализации порта (вход процесса); ядро только зовёт
`emit`.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.agent.control import CanUseToolRequest
from boba.agent.records import (
    PermissionMode,
    Record,
    TurnEndRecord,
    TurnOutcome,
)

__all__ = [
    "AgentEvent",
    "ControlCancelEvent",
    "ControlRequestEvent",
    "ErrorEvent",
    "EventSink",
    "HistoryWrittenEvent",
    "InitEvent",
    "RecordEvent",
    "ResultEvent",
    "StreamEvent",
    "ToolProgressEvent",
    "ToolStartedEvent",
]


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
    """Конец хода: его запись turn_end, уже сохранённая в истории. Агент
    шлёт событие, когда сессия свободна; приёмник рисует по записи то же,
    что воспроизведение истории."""

    turn_end: TurnEndRecord

    @property
    def outcome(self) -> TurnOutcome:
        return self.turn_end.outcome

    @property
    def num_turns(self) -> int:
        return self.turn_end.num_turns


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
    | ErrorEvent
    | ControlRequestEvent
    | ControlCancelEvent
)


class EventSink(Protocol):
    """Приёмник событий агента."""

    @abstractmethod
    async def emit(self, event: AgentEvent) -> None: ...
