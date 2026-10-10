"""Порт наружу: события хода, которые агент отдаёт запускающей программе.

Виды событий повторяют протокол `stream-json` из плана, раздел 5.18: первое
событие сеанса, кусок ответа, записанная запись разговора, итог хода и
ошибка вне хода. Как события уходят на провод — дело реализации порта
(вход процесса); ядро только зовёт `emit`.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict

from boba.agent.records import PermissionMode, Record, Usage

__all__ = [
    "AgentEvent",
    "ErrorEvent",
    "EventSink",
    "InitEvent",
    "RecordEvent",
    "ResultEvent",
    "StreamEvent",
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
    SHUTDOWN = "shutdown"


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
    """Кусок ответа модели по мере прихода."""

    content: str = ""
    reasoning: str = ""


class RecordEvent(EventModel):
    """Записана запись разговора."""

    record: Record


class ResultEvent(EventModel):
    """Конец хода."""

    outcome: TurnOutcome
    session_id: str
    usage: Usage
    num_turns: int
    duration_ms: int


class ErrorEvent(EventModel):
    """Ошибка вне хода: строка ввода не разобрана, сброс журнала не удался."""

    message: str
    unflushed: int = 0


AgentEvent: TypeAlias = InitEvent | StreamEvent | RecordEvent | ResultEvent | ErrorEvent


class EventSink(Protocol):
    """Приёмник событий агента."""

    @abstractmethod
    async def emit(self, event: AgentEvent) -> None: ...
