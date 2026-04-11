"""Доменные типы чата по документам — JSONL-хранилище.

Вся история чата — последовательность типизированных событий в JSONL.
Один обмен (вопрос → ответ) = группа событий с общим exchange_id.

Каждый этап pipeline дописывает своё событие в файл.

Взаимодействие с историей — через Writer / Reader:

    Запись:
        writer = JsonlChatWriter(path)
        eid = writer.new_exchange()
        writer.write(eid, EventType.USER, "вопрос")
        writer.write(eid, EventType.ASSISTANT, "ответ")

    Чтение:
        reader = JsonlChatReader(path)
        for event in reader.read():
            ...
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator

from domain.chat.growbuffer import GrowBuffer

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
        """Строковый ключ для JSONL-сериализации."""
        return self._key

    @property
    def label(self) -> str:
        """UI-метка для отображения."""
        return self._label

    @property
    def collapsible(self) -> bool:
        """Сворачивать ли в спойлер."""
        return self._collapsible

    @classmethod
    def from_key(cls, key: str) -> EventType:
        """Найти EventType по строковому ключу JSONL."""
        if not hasattr(cls, "_by_key"):
            cls._by_key = {et.key: et for et in cls}
        return cls._by_key[key]


# ---------------------------------------------------------------------------
# Модели метаданных для каждого типа события
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchHitMeta:
    """Метаданные одного найденного чанка (для хранения в JSONL)."""
    source_file: str
    start_line: int
    end_line: int
    score: float


@dataclass(frozen=True)
class SearchMeta:
    """Метаданные события поиска."""
    hits: list[SearchHitMeta] = field(default_factory=list)


@dataclass(frozen=True)
class ContextMeta:
    """Метаданные события контекста."""
    fragment_count: int = 0


# ---------------------------------------------------------------------------
# ChatEvent — чистый dataclass, без логики сериализации
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatEvent:
    """Одно событие в истории — тип + контент + привязка к обмену.

    content — основной текстовый контент события (вопрос, ответ, и т.д.).
    metadata — структурированные доп. данные (hits, fragment_count).
    """
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
# ChatEventSerializer — прослойка между ChatEvent и JSONL-текстом
# ---------------------------------------------------------------------------

class ChatEventDeserializeError(Exception):
    """Ошибка десериализации JSONL-строки в ChatEvent."""

    def __init__(self, line: str, reason: Exception) -> None:
        self.line = line
        self.reason = reason
        super().__init__(f"Cannot deserialize ChatEvent: {reason}")


class ChatEventSerializer:
    """Сериализация/десериализация ChatEvent ↔ JSONL-строка.

    Единственное место, знающее формат JSONL::

        line = ChatEventSerializer.serialize(event)
        event = ChatEventSerializer.deserialize(line)
    """

    LINE_SEPARATOR = "\n"
    LINE_SEPARATOR_BYTES = b"\n"

    class _Field:
        """Имена полей в JSONL-формате."""
        EXCHANGE_ID = "exchange_id"
        TYPE = "type"
        CONTENT = "content"
        TIMESTAMP = "ts"
        METADATA = "meta"

    @classmethod
    def serialize(cls, event: ChatEvent) -> str:
        """ChatEvent → JSONL-строка."""
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
        """JSONL-строка → ChatEvent.

        Raises:
            ChatEventDeserializeError: если строка не может быть разобрана.
        """
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


# ---------------------------------------------------------------------------
# JSONL Writer
# ---------------------------------------------------------------------------

class JsonlChatWriter:
    """Append-only writer для событий чата в JSONL.

    Единственная точка записи в файл истории::

        writer = JsonlChatWriter(path)
        eid = writer.new_exchange()
        writer.write(eid, EventType.USER, "вопрос")
    """

    _EXCHANGE_ID_LENGTH = 12

    def __init__(self, path: Path) -> None:
        self._path = path
        path.touch(exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def new_exchange(self) -> str:
        """Создать новый exchange_id."""
        return uuid.uuid4().hex[:self._EXCHANGE_ID_LENGTH]

    def write_event(self, event: ChatEvent) -> None:
        """Дописать событие в файл (append)."""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(ChatEventSerializer.serialize(event))
            f.write(ChatEventSerializer.LINE_SEPARATOR)

    def write(
        self,
        exchange_id: str,
        event_type: EventType,
        content: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        """Создать и записать событие."""
        event = ChatEvent(
            exchange_id=exchange_id,
            event_type=event_type,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )
        self.write_event(event)


# ---------------------------------------------------------------------------
# JSONL Reader
# ---------------------------------------------------------------------------

class JsonlChatReader:
    """Stateful reader для JSONL-истории чата.

    Владеет fd и GrowBuffer. Держит файл открытым на протяжении lifetime.
    Файл должен существовать к моменту создания reader
    (writer гарантирует это через touch).

    ``read()`` yield'ит события от текущей позиции до конца файла.
    ``rewind()`` сбрасывает позицию — следующий ``read()`` начнёт сначала.
    ``close()`` закрывает fd.

    Использование::

        with JsonlChatReader(path) as reader:
            for event in reader.read():
                renderer.render_event(event)

            writer.write(...)
            for event in reader.read():
                renderer.render_event(event)

            reader.rewind()
            for event in reader.read():
                ...
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._byte_pos = 0
        self._fd = open(path, "rb")
        self._buf = GrowBuffer(self._fd)

    def __enter__(self) -> JsonlChatReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Закрыть fd."""
        self._fd.close()

    @property
    def path(self) -> Path:
        return self._path

    def rewind(self) -> None:
        """Сбросить позицию в начало файла."""
        self._byte_pos = 0

    def read(self) -> Iterator[ChatEvent]:
        """Генератор событий от текущей позиции до конца файла.

        Обрабатывает только полные строки (до LINE_SEPARATOR).
        Неполная последняя строка остаётся — будет дочитана при следующем read().
        Битые строки логируются и пропускаются.
        """
        sep = ChatEventSerializer.LINE_SEPARATOR_BYTES
        for line_view in self._buf.iter_lines(sep, offset=self._byte_pos):
            line = bytes(line_view).decode("utf-8")
            if not line.strip():
                continue
            try:
                yield ChatEventSerializer.deserialize(line)
            except ChatEventDeserializeError:
                log.debug("Skipping malformed line in %s: %s", self._path, line.rstrip())

        self._byte_pos += self._buf.consumed
