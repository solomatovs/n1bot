"""View поверх HistoryService: AgentEvent -> DialogMessage.

HistoryService хранит сырые AgentEvent (phase / snapshot / advisory /
terminal). HistoryDialogView — абстрактный контракт восстановления
сообщений диалога между пользователем и ассистентом, то, что нужно
HistoryReducer для сборки dialog_messages в LLMRequest.

Реализации:
    AllHistoryDialogView
        Возвращает диалог целиком, без фильтрации по request_id.

    CompactHistoryDialogView
        Для текущего (последнего) request_id — полная цепочка
        выполнения (как AllHistoryDialogView). Для всех предыдущих
        request_id — только UserMessage и финальный
        AssistantMessage, очищенный до text-блоков. Промежуточные
        thinking/tool_calls/tool_results прошлых запросов в окно не
        попадают. Дополнительно применяется sliding-window
        max_messages: оставляет только последние сообщения, при этом
        сдвигает начало вправо до ближайшего UserMessage, чтобы
        контекст оставался валидным для LLM-провайдера.

Маппинг событий (общая логика):
    UserQueryReceived   -> UserMessage
    TotalMessage    -> AssistantMessage (источник истины: собранный message)
    ToolResultReady     -> ToolResultMessage (успех)
    ToolExecutionFailed -> ToolResultMessage (ошибка)

Per-field *Complete для реконструкции НЕ используются — message берётся
целиком из TotalMessage (порядко-независимо). Всё остальное (PhaseEvent /
FeedbackToLLMAdded / Terminal) игнорируется. FeedbackToLLMAdded пока
пропускается — событие неоднозначно (может быть UserMessage от LLMCritique или
ToolResultMessage от ToolCallRejection); семантику нужно расщеплять отдельно.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import ClassVar

from boba.agent.events import (
    AgentEvent,
    ToolExecutionFailed,
    ToolResultReady,
    TotalMessage,
    UserQueryReceived,
)
from boba.agent.history import HistoryReader
from boba.agent.models import ToolCallFailure
from boba.llm.models import (
    AssistantMessage,
    DialogMessage,
    RequestId,
    UserMessage,
)
from boba.llm.tool_result_render import tool_result_to_message
from boba.tools.domain import ErrorResult

__all__ = [
    "AllHistoryDialogView",
    "CompactHistoryDialogView",
    "HistoryDialogView",
]


class _DialogEventDecoder:
    """events -> DialogMessage. Источник истины ассистента — TotalMessage."""

    @classmethod
    def decode(cls, events: Iterable[AgentEvent]) -> Iterator[DialogMessage]:
        for event in events:
            match event:
                case UserQueryReceived(query=q):
                    yield UserMessage.from_text(q)

                case TotalMessage(message=msg):
                    # message собран консьюмером целиком — берём как есть.
                    if not msg.is_empty():
                        yield msg

                case ToolResultReady(call=call, result=result):
                    yield tool_result_to_message(
                        tool_call_id=call.id,
                        result=result.result,
                    )

                case ToolExecutionFailed(call=call, failure=failure):
                    yield tool_result_to_message(
                        tool_call_id=call.id,
                        result=cls._failure_to_error(failure),
                    )

                case _:
                    continue

    @staticmethod
    def _failure_to_error(failure: ToolCallFailure) -> ErrorResult:
        return ErrorResult(message=failure.message, error_kind=failure.error_kind)


class HistoryDialogView(ABC):
    """Контракт восстановления DialogMessage из журнала событий."""

    @abstractmethod
    def dialog_message_iter(self) -> Iterator[DialogMessage]:
        """Последовательность сообщений диалога в порядке регистрации."""
        ...


class AllHistoryDialogView(HistoryDialogView):
    """Полный диалог из журнала HistoryReader без фильтрации."""

    def __init__(self, history_reader: HistoryReader) -> None:
        self._history_reader = history_reader

    def dialog_message_iter(self) -> Iterator[DialogMessage]:
        yield from _DialogEventDecoder.decode(self._history_reader.events())


class CompactHistoryDialogView(HistoryDialogView):
    """Компактный диалог: полный текущий request_id + сжатые прошлые + окно.

    Для последнего по порядку появления request_id отдаёт цепочку
    выполнения без сокращений. Для каждого предыдущего request_id
    оставляет только UserMessage и финальный AssistantMessage,
    оставив в нём только text-блоки (thinking/refusal/tool_calls
    выбрасываются). Если у прошлого request_id text-блоков не было
    (упал, оборвался на tool_calls) — assistant-ответ опускается,
    остаётся только UserMessage.

    После сборки применяется sliding-window: оставляется не более
    max_messages сообщений с хвоста. Если граница окна попала в
    середину диалога — начало сдвигается вправо до ближайшего
    UserMessage, чтобы не передавать в LLM осиротевший
    ToolResultMessage.
    """

    _CONTRIBUTING_TYPES: ClassVar[tuple[type[AgentEvent], ...]] = (
        UserQueryReceived,
        TotalMessage,
        ToolResultReady,
        ToolExecutionFailed,
    )

    def __init__(self, history_reader: HistoryReader, max_messages: int) -> None:
        self._history_reader = history_reader
        self._max_messages = max_messages

    def dialog_message_iter(self) -> Iterator[DialogMessage]:
        messages = list(self._build_messages())
        yield from self._apply_window(messages)

    def _build_messages(self) -> Iterator[DialogMessage]:
        groups = self._group_by_request_id()
        if not groups:
            return
        last_request_id = next(reversed(groups))
        for request_id, events in groups.items():
            if request_id == last_request_id:
                yield from _DialogEventDecoder.decode(events)
            else:
                yield from self._compact_request(events)

    def _group_by_request_id(self) -> dict[RequestId, list[AgentEvent]]:
        groups: dict[RequestId, list[AgentEvent]] = {}
        for event in self._history_reader.events():
            if not isinstance(event, self._CONTRIBUTING_TYPES):
                continue
            groups.setdefault(event.request_id, []).append(event)
        return groups

    @staticmethod
    def _compact_request(events: list[AgentEvent]) -> Iterator[DialogMessage]:
        full = list(_DialogEventDecoder.decode(events))
        users = [m for m in full if isinstance(m, UserMessage)]
        assistants = [m for m in full if isinstance(m, AssistantMessage)]
        yield from users
        if not assistants:
            return
        # Прошлый turn сжимаем до текстового ответа (thinking/tool_calls — прочь).
        content = assistants[-1].content
        if content:
            yield AssistantMessage(content=content)

    def _apply_window(
        self,
        messages: list[DialogMessage],
    ) -> Iterator[DialogMessage]:
        if len(messages) <= self._max_messages:
            yield from messages
            return
        windowed = messages[-self._max_messages :]
        for index, message in enumerate(windowed):
            if isinstance(message, UserMessage):
                yield from windowed[index:]
                return
