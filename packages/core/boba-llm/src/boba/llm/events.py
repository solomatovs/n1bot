"""События LLM-слоя: от отправки запроса до finish_reason."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias, assert_never

from boba.llm.models import AssistantMessage, InvalidToolCall, RequestId, ToolCall

__all__ = [
    "BaseLLMEvent",
    "FinishReason",
    "LLMAnswerDelta",
    "LLMAnswerMessage",
    "LLMEvent",
    "LLMEventName",
    "LLMGenerationResult",
    "LLMInvalidToolCallMessage",
    "LLMRefusalDelta",
    "LLMRefusalMessage",
    "LLMSnapshot",
    "LLMStreamingDelta",
    "LLMThinkingDelta",
    "LLMThinkingMessage",
    "LLMToolCallDelta",
    "LLMToolCallMessage",
]


class FinishReason(StrEnum):
    """Нормализованная причина завершения генерации LLM.

    Перечень возможных значений и когда они приходят:

    - stop - модель сама решила, что закончила:
        - попала на EOS-токен своей токенизации
        - сгенерировала одну из stop-последовательностей из запроса
        - завершила свою мысль
      Прилетает на нормально завершённом текстовом ответе.
      У большинства нестрогих провайдеров (Ollama/qwen/llama)
      также часто прилетает в ситуации, когда модель эмитнула tool_calls
      это нарушение протокола, однако так работает в реалиях

    - length - генерация упёрлась в лимит токенов:
        - max_tokens из запроса
        - context window модели (prompt + output)
      Прилетает, когда вывод обрезан на полуслове.
      Восстановить нормальный текст нельзя — нужно либо поднимать лимит и
      перегенерировать, либо просить модель продолжить.

    - tool_calls - модель решила вызвать tool/function:
        - в стриме до finish_reason прошли tool_calls
        - в `message.tool_calls` лежит полный набор вызовов с
          собранными args
      Прилетает у строгих провайдеров (OpenAI Chat Completions, Anthropic
      tool_use) когда модель ожидает tool-result'ы и продолжение цикла.
      У мелких локальных моделей часто подменяется на stop — см. выше.

    - content_filter - провайдерский фильтр заблокировал генерацию:
        - модерация на стороне провайдера (Azure OpenAI content filter,
          OpenAI moderation, Anthropic safety)
        - сработал фильтр на запрос или на уже сгенерированный фрагмент
      Прилетает с потенциально пустым или обрезанным контентом.
      повторять с тем же промптом обычно бессмысленно, фильтр снова сработает.
    """

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"

    @property
    def is_terminal(self) -> bool:
        match self:
            case FinishReason.TOOL_CALLS:
                return False
            case FinishReason.STOP | FinishReason.LENGTH | FinishReason.CONTENT_FILTER:
                return True
            case _:
                assert_never(self)


@dataclass(frozen=True)
class BaseLLMEvent(ABC):
    """Базовый класс для всех событий LLM-слоя."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str: ...


@dataclass(frozen=True)
class LLMStreamingDelta(BaseLLMEvent, ABC):
    """Инкрементальный кусок контента в процессе генерации."""


@dataclass(frozen=True)
class LLMSnapshot(BaseLLMEvent, ABC):
    """Финализированный фрагмент ответа (агрегат delta-токенов одного типа)."""


@dataclass(frozen=True)
class LLMThinkingDelta(LLMStreamingDelta):
    """Chunk reasoning-токена."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMThinkingDelta"]:
        return "LLMThinkingDelta"


@dataclass(frozen=True)
class LLMAnswerDelta(LLMStreamingDelta):
    """Chunk текстового ответа."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMAnswerDelta"]:
        return "LLMAnswerDelta"


@dataclass(frozen=True)
class LLMRefusalDelta(LLMStreamingDelta):
    """Chunk отказа модели."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMRefusalDelta"]:
        return "LLMRefusalDelta"


@dataclass(frozen=True)
class LLMToolCallDelta(LLMStreamingDelta):
    """Chunk tool call: на первом появлении index несёт id+name и регистрирует
    слот (arguments может быть пустым), далее — фрагменты JSON args.

    Несёт маршрутную метаинформацию (index/id/name), неустранимую для tool
    call'а — потому шире текстовых *Delta, но из того же delta-семейства."""

    index: int
    tool_call_id: str
    tool_name: str
    arguments: str

    @classmethod
    def name(cls) -> Literal["LLMToolCallDelta"]:
        return "LLMToolCallDelta"


@dataclass(frozen=True)
class LLMThinkingMessage(LLMSnapshot):
    """Аггрегированный reasoning итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["LLMThinkingMessage"]:
        return "LLMThinkingMessage"


@dataclass(frozen=True)
class LLMAnswerMessage(LLMSnapshot):
    """Аггрегированный текстовый ответ итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["LLMAnswerMessage"]:
        return "LLMAnswerMessage"


@dataclass(frozen=True)
class LLMRefusalMessage(LLMSnapshot):
    """Аггрегированный отказ модели."""

    content: str

    @classmethod
    def name(cls) -> Literal["LLMRefusalMessage"]:
        return "LLMRefusalMessage"


@dataclass(frozen=True)
class LLMToolCallMessage(LLMSnapshot):
    """Завершённый tool call (id + имя + parsed args)."""

    call: ToolCall

    @classmethod
    def name(cls) -> Literal["LLMToolCallMessage"]:
        return "LLMToolCallMessage"


@dataclass(frozen=True)
class LLMInvalidToolCallMessage(LLMSnapshot):
    """LLM выдала tool-call с невалидным JSON в args."""

    invalid: InvalidToolCall

    @classmethod
    def name(cls) -> Literal["LLMInvalidToolCallMessage"]:
        return "LLMInvalidToolCallMessage"


@dataclass(frozen=True)
class LLMGenerationResult(LLMSnapshot):
    """
    Итог одной генерации LLM — собранный AssistantMessage и raw finish_reason.

    Консьюмер эмитит его последним — после всех per-field `*Message`
    событий; терминатор генерации (источник истины). Несёт всё, что пришло
    от провайдера в режиме streaming, как если бы это был `stream=False` ответ:

    - `message` — собранный AssistantMessage с плоскими полями
      (thinking / content / refusal / tool_calls / invalid_tool_calls).
      Пустой message — валидное состояние, когда модель
      завершилась без контента (например, `finish_reason=stop` без deltas).
    - `finish_reason` — то, что реально прислал провайдер; без подмен.

    Это единственное событие, на котором агент-слой принимает решение об
    остановке цикла — через отдельные `StopIf*` спецификации, скомпонованные
    через `.or_()` в `AgentBuilder`.
    """

    message: AssistantMessage
    finish_reason: FinishReason

    def __post_init__(self) -> None:
        if not isinstance(self.finish_reason, FinishReason):
            object.__setattr__(self, "finish_reason", FinishReason(self.finish_reason))

    @classmethod
    def name(cls) -> Literal["LLMGenerationResult"]:
        return "LLMGenerationResult"


LLMEvent = (
    LLMThinkingDelta
    | LLMAnswerDelta
    | LLMRefusalDelta
    | LLMToolCallDelta
    | LLMThinkingMessage
    | LLMAnswerMessage
    | LLMRefusalMessage
    | LLMToolCallMessage
    | LLMInvalidToolCallMessage
    | LLMGenerationResult
)


LLMEventName: TypeAlias = Literal[
    "LLMThinkingDelta",
    "LLMAnswerDelta",
    "LLMRefusalDelta",
    "LLMToolCallDelta",
    "LLMThinkingMessage",
    "LLMAnswerMessage",
    "LLMRefusalMessage",
    "LLMToolCallMessage",
    "LLMInvalidToolCallMessage",
    "LLMGenerationResult",
]


def _verify_llm_event_names_exhaustive(e: LLMEvent) -> LLMEventName:
    """Exhaustiveness check для match-case над LLMEvent."""
    return e.name()
