"""Порт хранения истории сообщений: разделение на Reader и Writer.

- MessageReader — read-side (message_iter / last). Использует HistoryReducer.
- MessageWriter — write-side (add / clear). Только DialogueWriter получает
  его через DI: инвариант «один писатель», поднятый с конвенции на типы.
- MessageService — композиция; реализации (in-memory, JSONL) отвечают
  на оба контракта, внешним консьюмерам отдают узкий интерфейс.

Контракт ошибок persistent-реализаций — потомки MessageStoreError.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from boba.domain.agent.events import AgentEvent, PersistenceFailed
from boba.domain.core.errors import TerminalError
from boba.domain.llm.models import LLMMessage, RequestId

__all__ = [
    "MessageReader",
    "MessageService",
    "MessageStoreError",
    "MessageStoreReadError",
    "MessageStoreWriteError",
    "MessageWriter",
]


class MessageStoreError(TerminalError[RequestId, AgentEvent]):
    """Базовая ошибка persistent-реализации MessageService.

    Контракт: persistent-реализации (файл, БД) ОБЯЗАНЫ оборачивать
    внутренние исключения (диск, сериализация) в потомков этого класса.
    Исходное исключение доступно через __cause__. In-memory
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


class MessageReader(ABC):
    """Read-side порт хранилища сообщений.

    Тип-маркер для consumer'ов, которым нужно только читать диалог:
    HistoryReducer собирает
    снапшот через message_iter для следующего LLMRequest.

    Никаких write-методов — даже если объект на самом деле
    MessageService (полная имплементация), reader-аннотация
    статически запрещает писать.
    """

    @abstractmethod
    def message_iter(self) -> Iterator[LLMMessage]:
        """Вернуть все сообщения в порядке добавления.

        Raises:
            MessageStoreReadError: persistent-реализация не смогла
                прочитать хранилище.
        """
        ...

    @abstractmethod
    def last(self) -> LLMMessage | None:
        """Последнее сообщение или None, если история пуста."""
        ...


class MessageWriter(ABC):
    """Write-side порт хранилища сообщений.

    Передаётся ТОЛЬКО \
DialogueWriter. Это закрытый канал записи: внешний код agent-слоя
    не должен иметь референса на MessageWriter, тем самым
    инвариант «единственный писатель — DialogueWriter» гарантируется
    типами, а не конвенцией.

    Контракт: add принимает сырой LLMMessage. Маппинг
    «доменная операция → LLMMessage» делает DialogueWriter; здесь
    интерфейс уже на transport-уровне.
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
    def clear(self) -> None:
        """Очистить всю историю.

        Raises:
            MessageStoreWriteError: persistent-реализация не смогла
                очистить хранилище.
        """
        ...


class MessageService(MessageReader, MessageWriter, ABC):
    """Композиция MessageReader + MessageWriter.

    Тип для *реализаций* (in-memory, JSONL): один объект держит
    хранилище и реализует оба контракта. Container получает реализацию,
    а наружу инжектирует НАРРОВ:

    - DialogueWriter принимает MessageWriter — может только
      писать;
    - HistoryReducer / LLMInvokeMiddleware принимают
      MessageReader — могут только читать.

    Имплементации могут быть как ephemeral (in-memory), так и
    persistent (диалог между запусками процесса восстанавливается
    реализацией самостоятельно при создании инстанса — без внешнего
    replay-слоя).

    Контракт ошибок: persistent-реализации ОБЯЗАНЫ наружу бросать
    только потомков MessageStoreError. Внутренние исключения
    (диск, сериализация) должны быть обёрнуты; исходное — через
    __cause__.
    """
