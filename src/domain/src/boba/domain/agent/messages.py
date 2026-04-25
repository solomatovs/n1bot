"""Порт сервиса управления историей сообщений + ошибки persistent-реализаций.

:class:`MessageService` хранит диалог (``system`` / ``user`` /
``assistant`` / ``tool`` сообщения) как append-log. Записи в сервис
делают в трёх местах:

1. :meth:`~boba.domain.agent.turn.spec.TurnSpec.initial` — применяет
   :class:`~boba.domain.agent.turn.effects.TurnEffect`-ы trigger'а
   в начале каждой итерации (user-query, tool-result, LLM feedback).
2. :class:`~boba.domain.agent.meat.dialogue.\
AssistantMessagePersistenceMiddleware` — записывает assistant-сообщение
   после :class:`~boba.domain.agent.events.GenerationDone`.
3. :class:`~boba.domain.agent.turn.reducers.HistoryReducer` — не пишет,
   а только **читает** ``message_iter()`` в ``TurnState.messages``
   при сборке следующего :class:`LLMRequest`.

In-memory реализация — :mod:`boba.adapters.in_memory_messages`,
persistent (JSONL) — :mod:`boba.adapters.jsonl_messages`.
Контракт ошибок persistent-реализаций — потомки
:class:`MessageStoreError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from boba.domain.agent.events import AgentEvent, PersistenceFailed
from boba.domain.core.errors import TerminalError
from boba.domain.llm.models import LLMMessage, RequestId


class MessageStoreError(TerminalError[RequestId, AgentEvent]):
    """Базовая ошибка persistent-реализации :class:`MessageService`.

    Контракт: persistent-реализации (файл, БД) ОБЯЗАНЫ оборачивать
    внутренние исключения (диск, сериализация) в потомков этого класса.
    Исходное исключение доступно через ``__cause__``. In-memory
    реализации ничего не бросают.
    """

    def __init__(self, reason: Exception, ctx: str = "") -> None:
        self.reason = reason
        self.ctx = ctx
        prefix = self._prefix()
        msg = f"{prefix}: {reason}"
        if ctx:
            msg = f"{prefix} ({ctx}): {reason}"
        super().__init__(msg)

    def _prefix(self) -> str:
        return "Message store error"

    def to_user_feedback(self, request_id: RequestId) -> PersistenceFailed:
        return PersistenceFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
        )


class MessageStoreWriteError(MessageStoreError):
    """Не удалось записать сообщение в хранилище."""

    def _prefix(self) -> str:
        return "Cannot write message"


class MessageStoreReadError(MessageStoreError):
    """Не удалось прочитать хранилище сообщений."""

    def _prefix(self) -> str:
        return "Cannot read messages"


class MessageService(ABC):
    """Абстрактный сервис управления историей сообщений.

    Реализации могут быть как ephemeral (in-memory), так и persistent
    (диалог между запусками процесса восстанавливается реализацией
    самостоятельно при создании инстанса — без внешнего replay-слоя).

    Контракт ошибок: persistent-реализации ОБЯЗАНЫ наружу бросать
    только потомков :class:`MessageStoreError`. Внутренние исключения
    (диск, сериализация) должны быть обёрнуты; исходное — через
    ``__cause__``.
    """

    @abstractmethod
    def add(self, message: LLMMessage) -> None:
        """Добавить сообщение в историю.

        Raises:
            MessageStoreWriteError: persistent-реализация не смогла
                записать сообщение в хранилище.
        """
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
        """Очистить всю историю.

        Raises:
            MessageStoreWriteError: persistent-реализация не смогла
                очистить хранилище.
        """
        ...
