"""
События SSE-потока хода агента:
    POST /api/sessions/{id}/chat/stream
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from boba.hermes.agent.models import Payload

logger = logging.getLogger(__name__)

__all__ = [
    "HermesAssistantCompleted",
    "HermesAssistantDelta",
    "HermesErrorEvent",
    "HermesEvent",
    "HermesEventCodec",
    "HermesEventName",
    "HermesEventStream",
    "HermesMessageStarted",
    "HermesRunCompleted",
    "HermesRunStarted",
    "HermesToolEvent",
    "HermesToolProgress",
    "HermesUsage",
]


class HermesEventName(StrEnum):
    """Имена событий из строки `event:` потока."""

    RUN_STARTED = "run.started"
    MESSAGE_STARTED = "message.started"
    ASSISTANT_DELTA = "assistant.delta"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_PROGRESS = "tool.progress"
    ASSISTANT_COMPLETED = "assistant.completed"
    RUN_COMPLETED = "run.completed"
    ERROR = "error"
    DONE = "done"


@dataclass(slots=True)
class HermesEvent(Payload):
    """Общая часть события: чей ход и в каком порядке пришло.

    Имя события в теле не приходит — оно живёт в строке `event:`, поэтому
    его проставляет декодер.
    """

    name: str = ""
    session_id: str | None = None
    run_id: str | None = None
    seq: int = 0
    ts: float = 0.0


@dataclass(slots=True)
class HermesRunStarted(HermesEvent):
    """Ход начат; сообщение пользователя вернулось так, как его принял hermes."""

    user_message: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HermesMessageStarted(HermesEvent):
    """Заведено сообщение ассистента: {"id": ..., "role": "assistant"}."""

    message: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HermesAssistantDelta(HermesEvent):
    """Кусок текста ответа."""

    message_id: str = ""
    delta: str = ""


@dataclass(slots=True)
class HermesToolEvent(HermesEvent):
    """Начало, конец или падение вызова инструмента.

    preview и args приходят только на tool.started: на tool.completed
    api_server шлёт их пустыми, результат вызова остаётся в истории сессии.
    """

    message_id: str = ""
    tool_name: str | None = None
    preview: str | None = None
    args: dict[str, Any] | None = None


@dataclass(slots=True)
class HermesToolProgress(HermesEvent):
    """Ход рассуждения; приходит под именем инструмента `_thinking`."""

    message_id: str = ""
    tool_name: str | None = None
    delta: str = ""


@dataclass(slots=True)
class HermesAssistantCompleted(HermesEvent):
    """Ответ собран целиком; content дублирует сумму дельт."""

    message_id: str = ""
    content: str = ""
    completed: bool = False
    partial: bool = False
    interrupted: bool = False
    runtime: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HermesUsage(Payload):
    """Расход токенов за ход."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class HermesRunCompleted(HermesEvent):
    """Ход завершён.

    messages — стенограмма хода в том виде, в каком её вернул провайдер
    (role/content/finish_reason/reasoning), без id и timestamp; те же
    сообщения уже лежат в истории сессии.
    """

    message_id: str = ""
    completed: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: HermesUsage = field(default_factory=HermesUsage)
    runtime: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "HermesRunCompleted":
        """Помимо общего разбора собирает вложенный usage."""
        event = cls._known_of({k: v for k, v in payload.items() if k != "usage"})
        usage = payload.get("usage")
        if isinstance(usage, dict):
            event.usage = HermesUsage.from_api(usage)

        return event


@dataclass(slots=True)
class HermesErrorEvent(HermesEvent):
    """Ход упал: текст ошибки от api_server."""
    message: str = ""


class HermesEventCodec:
    """Кадр SSE-потока в модель события."""

    _TYPES: ClassVar[dict[str, type[HermesEvent]]] = {
        HermesEventName.RUN_STARTED: HermesRunStarted,
        HermesEventName.MESSAGE_STARTED: HermesMessageStarted,
        HermesEventName.ASSISTANT_DELTA: HermesAssistantDelta,
        HermesEventName.TOOL_STARTED: HermesToolEvent,
        HermesEventName.TOOL_COMPLETED: HermesToolEvent,
        HermesEventName.TOOL_FAILED: HermesToolEvent,
        HermesEventName.TOOL_PROGRESS: HermesToolProgress,
        HermesEventName.ASSISTANT_COMPLETED: HermesAssistantCompleted,
        HermesEventName.RUN_COMPLETED: HermesRunCompleted,
        HermesEventName.ERROR: HermesErrorEvent,
        HermesEventName.DONE: HermesEvent,
    }

    def of(self, name: str, data: str) -> HermesEvent | None:
        """Событие из имени и json-тела; None, если кадр нечитаем."""
        payload = self._payload(data)
        if payload is None:
            return None

        event = self._TYPES.get(name, HermesEvent).from_api(payload)
        event.name = name

        return event

    @staticmethod
    def _payload(data: str) -> dict[str, Any] | None:
        """Тело кадра; None, если это не json-объект."""
        try:
            payload = json.loads(data)
        except ValueError:
            logger.warning("unreadable hermes event body: %r", data[:200])
            return None

        if not isinstance(payload, dict):
            logger.warning("unexpected hermes event body: %r", data[:200])
            return None

        return payload


class HermesEventStream:
    """Строки SSE в события: кадр собирается до пустой строки.

    Строки, начинающиеся с двоеточия (`: keepalive`, `: stream closed`), —
    комментарии протокола, они кадр не открывают и не закрывают.
    """

    def __init__(self) -> None:
        self._codec = HermesEventCodec()

    async def of(self, lines: AsyncIterator[str]) -> AsyncIterator[HermesEvent]:
        """События потока в порядке прихода."""
        name = ""
        data: list[str] = []

        async for raw in lines:
            line = raw.rstrip("\r")

            if not line:
                if event := self._event_of(name, data):
                    yield event
                name, data = "", []
                continue

            if line.startswith(":"):
                continue

            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data.append(line.removeprefix("data:").strip())

        if event := self._event_of(name, data):
            yield event

    def _event_of(self, name: str, data: list[str]) -> HermesEvent | None:
        """Собранный кадр в событие; None, если кадр пуст или нечитаем."""
        if not name or not data:
            return None

        return self._codec.of(name, "\n".join(data))
