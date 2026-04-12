"""Доменные типы чата — EventType, ChatEvent, сериализация.

Чистые value objects и enum'ы. Без I/O, без файловой системы.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы событий
# ---------------------------------------------------------------------------


class EventType(Enum):
    """Тип события в истории чата.

    value — кортеж (ключ JSONL, UI-метка, сворачиваемость).
    """

    USER = ("user", "Вопрос", False)
    SEARCH = ("search", "Найденные фрагменты", True)
    CONTEXT = ("context", "Контекст из документов", True)
    THINKING = ("thinking", "Размышления", True)
    ASSISTANT = ("assistant", "Ответ", False)
    TOOL_CALL = ("tool_call", "Вызов инструмента", True)
    TOOL_RESULT = ("tool_result", "Результат инструмента", True)

    def __init__(self, key: str, label: str, collapsible: bool) -> None:
        self._key = key
        self._label = label
        self._collapsible = collapsible

    @property
    def key(self) -> str:
        return self._key

    @property
    def label(self) -> str:
        return self._label

    @property
    def collapsible(self) -> bool:
        return self._collapsible

    @classmethod
    def from_key(cls, key: str) -> EventType:
        if not hasattr(cls, "_by_key"):
            cls._by_key = {et.key: et for et in cls}
        return cls._by_key[key]


# ---------------------------------------------------------------------------
# ChatEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatEvent:
    """Одно событие в истории — тип + контент + привязка к обмену."""

    exchange_id: str
    event_type: EventType
    content: str
    timestamp: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_user(self) -> bool:
        return self.event_type is EventType.USER

    @property
    def is_assistant(self) -> bool:
        return self.event_type is EventType.ASSISTANT


# ---------------------------------------------------------------------------
# Метаданные
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHitMeta:
    source_file: str
    start_line: int
    end_line: int
    score: float


@dataclass(frozen=True)
class SearchMeta:
    hits: list[SearchHitMeta] = field(default_factory=list)


@dataclass(frozen=True)
class ContextMeta:
    fragment_count: int = 0


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------


class ChatEventDeserializeError(Exception):
    def __init__(self, line: str, reason: Exception) -> None:
        self.line = line
        self.reason = reason
        super().__init__(f"Cannot deserialize ChatEvent: {reason}")


class ChatEventSerializer:
    """Сериализация/десериализация ChatEvent ↔ JSONL-строка."""

    LINE_SEPARATOR = "\n"
    LINE_SEPARATOR_BYTES = b"\n"

    class _Field:
        EXCHANGE_ID = "exchange_id"
        TYPE = "type"
        CONTENT = "content"
        TIMESTAMP = "ts"
        METADATA = "meta"

    @classmethod
    def serialize(cls, event: ChatEvent) -> str:
        F = cls._Field
        result: Dict[str, Any] = {
            F.EXCHANGE_ID: event.exchange_id,
            F.TYPE: event.event_type.key,
            F.CONTENT: event.content,
            F.TIMESTAMP: event.timestamp,
        }
        if event.metadata:
            result[F.METADATA] = event.metadata
        return json.dumps(result, ensure_ascii=False)

    @classmethod
    def deserialize(cls, line: str) -> ChatEvent:
        try:
            raw = json.loads(line)
            F = cls._Field
            return ChatEvent(
                exchange_id=raw[F.EXCHANGE_ID],
                event_type=EventType.from_key(raw[F.TYPE]),
                content=raw.get(F.CONTENT, ""),
                timestamp=raw.get(F.TIMESTAMP, ""),
                metadata=raw.get(F.METADATA, {}),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ChatEventDeserializeError(line, exc) from exc
