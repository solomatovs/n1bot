"""JSONL-реализация ChatWriter/ChatReader.

Инфраструктурный адаптер: файловая система + JSONL формат.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

from adapters.growbuffer import GrowBuffer
from domain.chat.events import ChatEvent, ChatEventDeserializeError, ChatEventSerializer, EventType
from domain.core.storage import ChatReader, ChatWriter

log = logging.getLogger(__name__)


class JsonlChatWriter(ChatWriter[ChatEvent]):
    """Append-only writer для событий чата в JSONL."""

    _EXCHANGE_ID_LENGTH = 12

    def __init__(self, path: Path) -> None:
        self._path = path
        path.touch(exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def new_exchange(self) -> str:
        return uuid.uuid4().hex[:self._EXCHANGE_ID_LENGTH]

    def write_event(self, event: ChatEvent) -> None:
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
        event = ChatEvent(
            exchange_id=exchange_id,
            event_type=event_type,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )
        self.write_event(event)


class JsonlChatReader(ChatReader[ChatEvent]):
    """Stateful reader для JSONL-истории чата.

    Владеет fd и GrowBuffer. Держит файл открытым на протяжении lifetime.

        with JsonlChatReader(path) as reader:
            for event in reader.read():
                ...
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._byte_pos = 0
        self._fd = open(path, "rb")
        self._buf = GrowBuffer(self._fd)

    def close(self) -> None:
        self._fd.close()

    @property
    def path(self) -> Path:
        return self._path

    def rewind(self) -> None:
        self._byte_pos = 0

    def read(self) -> Iterator[ChatEvent]:
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
