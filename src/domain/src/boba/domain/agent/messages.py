"""Порт сервиса управления историей сообщений."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from boba.domain.agent.errors import AgentTerminalError
from boba.domain.agent.events import AgentEvent, PersistenceFailed
from boba.domain.agent.models import LLMMessage, RequestId
from boba.domain.core.errors import Retryable


class MessageStoreError(AgentTerminalError):
    """Базовая ошибка persistent-реализации :class:`MessageService`.

    Контракт ошибок: если реализация персистит диалог (например, в файл),
    любые внутренние исключения (ошибки хранилища, сериализации и т.п.)
    ОБЯЗАНЫ оборачиваться в потомков этого класса. Исходное исключение
    доступно через ``__cause__``. In-memory реализации ничего не бросают —
    это актуально только там, где есть внешнее хранилище.
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

    def to_user_event(self, request_id: RequestId) -> AgentEvent:
        return PersistenceFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            retryable=isinstance(self, Retryable),
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
    """
    Абстрактный сервис управления историей сообщений для модели.

    Реализации могут быть как эпhemeral (in-memory), так и persistent
    (диалог между запусками процесса восстанавливается реализацией
    самостоятельно при создании инстанса — без внешнего replay-слоя).

    Контракт ошибок: persistent-реализации ОБЯЗАНЫ наружу бросать только
    потомков :class:`MessageStoreError`. Внутренние исключения (диск,
    сериализация) должны быть обёрнуты; исходное — через ``__cause__``.
    """

    @abstractmethod
    def add(self, message: LLMMessage) -> None:
        """Добавить сообщение в историю.

        Raises:
            MessageStoreWriteError: у persistent-реализации не удалось
                записать сообщение в хранилище.
        """
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
        """Очистить всю историю.

        Raises:
            MessageStoreWriteError: у persistent-реализации не удалось
                очистить хранилище.
        """
        ...
