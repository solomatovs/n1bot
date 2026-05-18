"""View поверх HistoryService: AgentEvent → DialogMessage.

HistoryService хранит сырые `AgentEvent` (phase / snapshot / advisory /
terminal). `HistoryDialogView` фильтрует этот поток и восстанавливает
сообщения диалога между пользователем и ассистентом — то, что нужно
`HistoryReducer` для сборки `dialog_messages` в `LLMRequest`.

Маппинг:
    UserQueryReceived            → UserMessage
    {Thinking,Answer,Refusal}Complete
    + ToolCallComplete
    + InvalidToolCallReceived    → копятся в AssistantMessage
    GenerationDone               → flush AssistantMessage
    ToolResultReady              → ToolResultMessage (успех)
    ToolExecutionFailed          → ToolResultMessage (ошибка)

Всё остальное (PhaseEvent / FeedbackToLLMAdded / Terminal) игнорируется.
FeedbackToLLMAdded пока пропускается — событие неоднозначно (может быть
UserMessage от LLMCritique или ToolResultMessage от ToolCallRejection);
семантику нужно расщеплять отдельным шагом.

Группировка блоков AssistantMessage идёт по `(request_id, iteration)`:
снапшоты ассистента эмитятся `AssistantAggregator` в LLM-слое в фиксированном
порядке (thinking → answer → refusal → tool_calls → invalid), а
`GenerationDone` закрывает генерацию.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from boba.agent.events import (
    AnswerComplete,
    GenerationDone,
    InvalidToolCallReceived,
    RefusalComplete,
    ThinkingComplete,
    ToolCallComplete,
    ToolExecutionFailed,
    ToolResultReady,
    UserQueryReceived,
)
from boba.agent.history import HistoryReader
from boba.agent.models import ToolCallFailure
from boba.llm.models import (
    AssistantBlock,
    AssistantMessage,
    DialogMessage,
    InvalidToolCallBlock,
    RefusalBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    UserMessage,
    new_message_id,
)
from boba.llm.tool_result_render import tool_result_to_message
from boba.tools.domain import ErrorResult

__all__ = ["HistoryDialogView"]


@dataclass
class _AssistantAccumulator:
    """Буфер блоков AssistantMessage в порядке прихода снапшотов."""

    blocks: list[AssistantBlock] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.blocks

    def flush(self) -> AssistantMessage:
        return AssistantMessage(id=new_message_id(), blocks=tuple(self.blocks))


class HistoryDialogView:
    """Восстанавливает поток `DialogMessage` из журнала `HistoryReader`."""

    def __init__(self, history_reader: HistoryReader) -> None:
        self._history_reader = history_reader

    def dialog_message_iter(self) -> Iterator[DialogMessage]:  # noqa: C901
        accumulator = _AssistantAccumulator()

        for event in self._history_reader.events():
            match event:
                case UserQueryReceived(query=q):
                    yield from self._flush_assistant(accumulator)
                    yield UserMessage.from_text(q)

                case ThinkingComplete(content=c):
                    accumulator.blocks.append(ThinkingBlock(content=c))

                case AnswerComplete(content=c):
                    accumulator.blocks.append(TextBlock(content=c))

                case RefusalComplete(content=c):
                    accumulator.blocks.append(RefusalBlock(content=c))

                case ToolCallComplete(call=call):
                    accumulator.blocks.append(ToolCallBlock(call=call))

                case InvalidToolCallReceived(invalid=invalid):
                    accumulator.blocks.append(InvalidToolCallBlock(invalid=invalid))

                case GenerationDone():
                    yield from self._flush_assistant(accumulator)

                case ToolResultReady(call=call, result=result):
                    yield from self._flush_assistant(accumulator)
                    yield tool_result_to_message(
                        tool_call_id=call.id,
                        result=result.result,
                    )

                case ToolExecutionFailed(call=call, failure=failure):
                    yield from self._flush_assistant(accumulator)
                    yield tool_result_to_message(
                        tool_call_id=call.id,
                        result=_failure_to_error(failure),
                    )

                case _:
                    continue

        yield from self._flush_assistant(accumulator)

    @staticmethod
    def _flush_assistant(
        accumulator: _AssistantAccumulator,
    ) -> Iterator[AssistantMessage]:
        if accumulator.is_empty():
            return
        yield accumulator.flush()
        accumulator.blocks.clear()


def _failure_to_error(failure: ToolCallFailure) -> ErrorResult:
    return ErrorResult(message=failure.message, error_kind=failure.error_kind)
