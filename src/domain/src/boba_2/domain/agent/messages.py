"""Порт сервиса управления историей сообщений.

:class:`MessageService` хранит диалог (``system`` / ``user`` /
``assistant`` / ``tool`` сообщения) как append-log. Нужен для двух
вещей:

1. **Межзапросная память**. ``UserMessagePersistenceMiddleware``
   кладёт запрос пользователя один раз (прямо перед отправкой в
   LLM); каждая итерация агентского цикла подтягивает всю историю
   в ``ctx.request.history_messages`` через
   :class:`~boba_2.domain.agent.meat.history.HistoryMiddleware`.
2. **Межитерационная память**. После ответа модели
   :class:`~boba_2.domain.agent.meat.dialogue.\
AssistantMessagePersistenceMiddleware` пишет assistant-сообщение в
   сервис; следующая итерация (с tool-результатом) увидит его в
   истории.

S4b — только абстрактный порт + простая in-memory-реализация в
:mod:`boba_2.adapters.in_memory_messages`. Persistent-реализации
(``jsonl``-лог на диске, база) и полноценная иерархия
:class:`MessageStoreError` появятся при миграции соответствующих
кусков из старого кода.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from boba_2.domain.llm.models import LLMMessage


class MessageService(ABC):
    """Абстрактный сервис управления историей сообщений."""

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
        """Последнее сообщение или ``None``, если история пуста."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Очистить всю историю."""
        ...
