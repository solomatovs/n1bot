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
    "LLMAnswerComplete",
    "LLMAnswerStarted",
    "LLMAnswerToken",
    "LLMEvent",
    "LLMEventName",
    "LLMGenerationDone",
    "LLMGenerationResult",
    "LLMGenerationStarted",
    "LLMInvalidToolCallReceived",
    "LLMLifecycleMarker",
    "LLMRefusalComplete",
    "LLMRefusalToken",
    "LLMRequestStarted",
    "LLMResponseStarted",
    "LLMRetryAttempt",
    "LLMSnapshot",
    "LLMStreamingDelta",
    "LLMThinkingComplete",
    "LLMThinkingStarted",
    "LLMThinkingToken",
    "LLMToolCallArgumentDelta",
    "LLMToolCallBegin",
    "LLMToolCallComplete",
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
class LLMLifecycleMarker(BaseLLMEvent, ABC):
    """Граница фазы (старт/конец стадии, без контента)."""


@dataclass(frozen=True)
class LLMStreamingDelta(BaseLLMEvent, ABC):
    """Инкрементальный кусок контента между *Started и *Done."""


@dataclass(frozen=True)
class LLMSnapshot(BaseLLMEvent, ABC):
    """Финализированный фрагмент ответа (агрегат delta-токенов одного типа)."""


@dataclass(frozen=True)
class LLMRequestStarted(LLMLifecycleMarker):
    """
    Событие возникает непосредственно перед отправкой HTTP-запрос к llm
    """

    model: str
    messages_count: int
    has_tools: bool
    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestStarted"]:
        return "LLMRequestStarted"


@dataclass(frozen=True)
class LLMResponseStarted(LLMLifecycleMarker):
    """
    Событие возникает непосредственно после получения HTTP-ответа от llm
    Данные еще не прочитаны
    Разница по времени между
    `LLMResponseStarted.monotonic_ns` - `LLMRequestStarted.monotonic_ns`
    Позволяет замерить скорость обработки запроса самой llm
    Либо самим провайдером предоставляющим доступ к llm
    """

    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestSent"]:
        return "LLMRequestSent"


@dataclass(frozen=True)
class LLMGenerationStarted(LLMLifecycleMarker):
    """Первый chunk получен."""

    @classmethod
    def name(cls) -> Literal["LLMGenerationStarted"]:
        return "LLMGenerationStarted"


@dataclass(frozen=True)
class LLMThinkingStarted(LLMLifecycleMarker):
    """Модель начала reasoning."""

    @classmethod
    def name(cls) -> Literal["LLMThinkingStarted"]:
        return "LLMThinkingStarted"


@dataclass(frozen=True)
class LLMThinkingToken(LLMStreamingDelta):
    """Chunk reasoning-токена."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMThinkingToken"]:
        return "LLMThinkingToken"


@dataclass(frozen=True)
class LLMAnswerStarted(LLMLifecycleMarker):
    """Модель начала отдавать ответ."""

    @classmethod
    def name(cls) -> Literal["LLMAnswerStarted"]:
        return "LLMAnswerStarted"


@dataclass(frozen=True)
class LLMAnswerToken(LLMStreamingDelta):
    """Chunk текстового ответа."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMAnswerToken"]:
        return "LLMAnswerToken"


@dataclass(frozen=True)
class LLMRefusalToken(LLMStreamingDelta):
    """Chunk отказа модели."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMRefusalToken"]:
        return "LLMRefusalToken"


@dataclass(frozen=True)
class LLMToolCallBegin(LLMLifecycleMarker):
    """Начало tool call — id и имя функции пришли."""

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["LLMToolCallBegin"]:
        return "LLMToolCallBegin"


@dataclass(frozen=True)
class LLMToolCallArgumentDelta(LLMStreamingDelta):
    """Chunk аргументов tool call."""

    index: int
    tool_call_id: str
    tool_name: str
    arguments: str

    @classmethod
    def name(cls) -> Literal["LLMToolCallArgumentDelta"]:
        return "LLMToolCallArgumentDelta"


@dataclass(frozen=True)
class LLMRetryAttempt(LLMLifecycleMarker):
    """Попытка запроса к LLM будет повторена."""

    attempt: int
    reason: str
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["LLMRetryAttempt"]:
        return "LLMRetryAttempt"


@dataclass(frozen=True)
class LLMGenerationDone(LLMLifecycleMarker):
    """Генерация завершена — пришёл finish_reason.

    Поле `finish_reason` — то, что реально прислал провайдер на wire,
    без подмен. Решения о терминальности цикла принимаются выше — там,
    где есть доступ к собранному `LLMGenerationResult`.
    """

    finish_reason: FinishReason = FinishReason.STOP

    def __post_init__(self) -> None:
        if not isinstance(self.finish_reason, FinishReason):
            object.__setattr__(self, "finish_reason", FinishReason(self.finish_reason))

    @classmethod
    def name(cls) -> Literal["LLMGenerationDone"]:
        return "LLMGenerationDone"


@dataclass(frozen=True)
class LLMThinkingComplete(LLMSnapshot):
    """Аггрегированный reasoning итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["LLMThinkingComplete"]:
        return "LLMThinkingComplete"


@dataclass(frozen=True)
class LLMAnswerComplete(LLMSnapshot):
    """Аггрегированный текстовый ответ итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["LLMAnswerComplete"]:
        return "LLMAnswerComplete"


@dataclass(frozen=True)
class LLMRefusalComplete(LLMSnapshot):
    """Аггрегированный отказ модели."""

    content: str

    @classmethod
    def name(cls) -> Literal["LLMRefusalComplete"]:
        return "LLMRefusalComplete"


@dataclass(frozen=True)
class LLMToolCallComplete(LLMSnapshot):
    """Завершённый tool call (id + имя + parsed args)."""

    call: ToolCall

    @classmethod
    def name(cls) -> Literal["LLMToolCallComplete"]:
        return "LLMToolCallComplete"


@dataclass(frozen=True)
class LLMInvalidToolCallReceived(LLMSnapshot):
    """LLM выдала tool-call с невалидным JSON в args."""

    invalid: InvalidToolCall

    @classmethod
    def name(cls) -> Literal["LLMInvalidToolCallReceived"]:
        return "LLMInvalidToolCallReceived"


@dataclass(frozen=True)
class LLMGenerationResult(LLMSnapshot):
    """Итог одной генерации LLM — собранный AssistantMessage и raw finish_reason.

    Эмитится `AssistantAggregator` строго после `LLMGenerationDone` и после
    всех per-field `*Complete` событий. Несёт всё, что пришло от провайдера
    в режиме streaming, как если бы это был `stream=False` ответ:

    - `message` — собранный AssistantMessage со всеми блоками
      (thinking / text / refusal / tool_calls / invalid_tool_calls).
      Пустой message (без блоков) — валидное состояние, когда модель
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
    LLMRequestStarted
    | LLMResponseStarted
    | LLMGenerationStarted
    | LLMThinkingStarted
    | LLMThinkingToken
    | LLMAnswerStarted
    | LLMAnswerToken
    | LLMRefusalToken
    | LLMToolCallBegin
    | LLMToolCallArgumentDelta
    | LLMRetryAttempt
    | LLMGenerationDone
    | LLMThinkingComplete
    | LLMAnswerComplete
    | LLMRefusalComplete
    | LLMToolCallComplete
    | LLMInvalidToolCallReceived
    | LLMGenerationResult
)


LLMEventName: TypeAlias = Literal[
    "LLMRequestStarted",
    "LLMRequestSent",
    "LLMGenerationStarted",
    "LLMThinkingStarted",
    "LLMThinkingToken",
    "LLMAnswerStarted",
    "LLMAnswerToken",
    "LLMRefusalToken",
    "LLMToolCallBegin",
    "LLMToolCallArgumentDelta",
    "LLMRetryAttempt",
    "LLMGenerationDone",
    "LLMThinkingComplete",
    "LLMAnswerComplete",
    "LLMRefusalComplete",
    "LLMToolCallComplete",
    "LLMInvalidToolCallReceived",
    "LLMGenerationResult",
]


def _verify_llm_event_names_exhaustive(e: LLMEvent) -> LLMEventName:
    """Exhaustiveness check для match-case над LLMEvent."""
    return e.name()
