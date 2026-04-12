"""ABC для хранения истории чата.

ChatWriter — append-only запись событий.
ChatReader — streaming чтение событий, context manager.

Реализации: JsonlChatWriter, JsonlChatReader (JSONL файлы).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Iterator, TypeVar

TEvent = TypeVar("TEvent")


class ChatWriter(ABC, Generic[TEvent]):
    """Append-only запись событий чата."""

    @abstractmethod
    def new_exchange(self) -> str:
        """Создать новый exchange_id."""

    @abstractmethod
    def write_event(self, event: TEvent) -> None:
        """Дописать событие."""


class ChatReader(ABC, Generic[TEvent]):
    """Streaming чтение событий чата. Context manager."""

    @abstractmethod
    def read(self) -> Iterator[TEvent]:
        """Yield события от текущей позиции."""

    @abstractmethod
    def rewind(self) -> None:
        """Сбросить позицию в начало."""

    @abstractmethod
    def close(self) -> None:
        """Закрыть ресурсы."""

    def __enter__(self) -> ChatReader[TEvent]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
