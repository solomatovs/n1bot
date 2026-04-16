"""Порт сервиса управления историей сообщений."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from boba.domain.agent.models import LLMMessage


class MessageService(ABC):
    """
    Абстрактный сервис управления историей сообщений.
    Единственный способ добавлять/читать/удалять messages в рамках сессии.
    """

    @abstractmethod
    def add(self, message: LLMMessage) -> None:
        """Добавить сообщение в историю."""
        ...

    @abstractmethod
    def message_iter(self) -> Iterator[LLMMessage]:
        """Вернуть все сообщения в порядке добавления."""
        ...

    @abstractmethod
    def last(self) -> LLMMessage | None:
        """Последнее сообщение или None."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Очистить всю историю."""
        ...
