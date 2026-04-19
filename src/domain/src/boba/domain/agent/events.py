from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, TypeAlias

from boba.domain.agent.errors import UserNoticeSeverity
from boba.domain.agent.models import RequestId
from boba.domain.core.patterns import Converter, Serializer


@dataclass(frozen=True)
class BaseEvent(ABC):
    """Базовый класс для всех событий агента."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Стабильное имя типа события для сериализации.

        Каждый подкласс **должен** возвращать свою ``Literal["..."]``-строку
        (не просто ``str``): это позволяет статическому анализатору проверять
        исчерпывающее покрытие match-case в декодерах.
        """
        ...


@dataclass(frozen=True)
class UserQueryReceived(BaseEvent):
    """Запрос пользователя принят."""

    query: str

    @classmethod
    def name(cls) -> Literal["UserQueryReceived"]:
        return "UserQueryReceived"


@dataclass(frozen=True)
class StageStarted(BaseEvent):
    stage: str

    @classmethod
    def name(cls) -> Literal["StageStarted"]:
        return "StageStarted"


@dataclass(frozen=True)
class StageCompleted(BaseEvent):
    stage: str
    detail: str

    @classmethod
    def name(cls) -> Literal["StageCompleted"]:
        return "StageCompleted"


@dataclass(frozen=True)
class GenerationStarted(BaseEvent):
    """Первый chunk от LLM — генерация началась."""

    @classmethod
    def name(cls) -> Literal["GenerationStarted"]:
        return "GenerationStarted"


@dataclass(frozen=True)
class ThinkingStarted(BaseEvent):
    """Модель начала thinking/reasoning."""

    @classmethod
    def name(cls) -> Literal["ThinkingStarted"]:
        return "ThinkingStarted"


@dataclass(frozen=True)
class ThinkingToken(BaseEvent):
    """Chunk thinking/reasoning от LLM."""

    token: str

    @classmethod
    def name(cls) -> Literal["ThinkingToken"]:
        return "ThinkingToken"


@dataclass(frozen=True)
class AnswerStarted(BaseEvent):
    """Модель начала генерировать ответ."""

    @classmethod
    def name(cls) -> Literal["AnswerStarted"]:
        return "AnswerStarted"


@dataclass(frozen=True)
class AnswerToken(BaseEvent):
    """Chunk текстового ответа от LLM."""

    token: str

    @classmethod
    def name(cls) -> Literal["AnswerToken"]:
        return "AnswerToken"


@dataclass(frozen=True)
class RefusalToken(BaseEvent):
    """Chunk отказа модели отвечать."""

    token: str

    @classmethod
    def name(cls) -> Literal["RefusalToken"]:
        return "RefusalToken"


@dataclass(frozen=True)
class GenerationDone(BaseEvent):
    """Генерация завершена."""

    finish_reason: str = "stop"  # "stop", "tool_calls", "length"

    @classmethod
    def name(cls) -> Literal["GenerationDone"]:
        return "GenerationDone"


@dataclass(frozen=True)
class GenerationFailed(BaseEvent):
    """Терминальный отказ: адаптер/retry не смогли получить ответ LLM."""

    error_kind: str
    message: str
    retryable: bool
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["GenerationFailed"]:
        return "GenerationFailed"


@dataclass(frozen=True)
class PromptFailed(BaseEvent):
    """Терминальный отказ: PromptFactory/провайдер не смогли собрать промпт."""

    error_kind: str
    message: str
    retryable: bool
    provider: str | None = None

    @classmethod
    def name(cls) -> Literal["PromptFailed"]:
        return "PromptFailed"


@dataclass(frozen=True)
class PersistenceFailed(BaseEvent):
    """Терминальный отказ: не удалось прочитать/записать журнал/хранилище."""

    error_kind: str
    message: str
    retryable: bool

    @classmethod
    def name(cls) -> Literal["PersistenceFailed"]:
        return "PersistenceFailed"


@dataclass(frozen=True)
class ToolCallBegin(BaseEvent):
    """Начало tool call — пришёл id и имя функции."""

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolCallBegin"]:
        return "ToolCallBegin"


@dataclass(frozen=True)
class ToolCallArgumentDelta(BaseEvent):
    """Chunk аргументов tool call."""

    index: int
    arguments: str

    @classmethod
    def name(cls) -> Literal["ToolCallArgumentDelta"]:
        return "ToolCallArgumentDelta"


@dataclass(frozen=True)
class ToolCallComplete(BaseEvent):
    """Агрегированный tool call: имя + полные аргументы."""

    tool_call_id: str
    tool_name: str
    arguments: str

    @classmethod
    def name(cls) -> Literal["ToolCallComplete"]:
        return "ToolCallComplete"


@dataclass(frozen=True)
class ToolResultReady(BaseEvent):
    """Результат успешного выполнения tool.

    Ошибки сюда не попадают — для них есть :class:`ToolExecutionFailed`.
    """

    tool_call_id: str
    tool_name: str
    content: str

    @classmethod
    def name(cls) -> Literal["ToolResultReady"]:
        return "ToolResultReady"


@dataclass(frozen=True)
class UserNoticeReady(BaseEvent):
    """Нотис для пользователя (не для LLM).

    Эмитится роутером из :class:`UserNoticeError`. Sink'и UI отрисуют
    сообщение по ``severity`` (info / warning / error). Не терминальное —
    цикл агента продолжается. В :class:`MessageService` не попадает.
    """

    message: str
    severity: UserNoticeSeverity

    @classmethod
    def name(cls) -> Literal["UserNoticeReady"]:
        return "UserNoticeReady"


@dataclass(frozen=True)
class ToolExecutionFailed(BaseEvent):
    """Ошибка выполнения tool.

    Не терминальное событие: цикл агента продолжается. Middleware уже
    записал ``message`` в ``MessageService`` как ``LLMMessage(role="tool")``,
    чтобы LLM на следующей итерации увидела ошибку и могла её починить.
    Событие нужно sink'ам (UI/журнал), чтобы отрисовать/залогировать факт
    ошибки отдельно от успешного результата.
    """

    tool_call_id: str
    tool_name: str
    error_kind: str
    message: str

    @classmethod
    def name(cls) -> Literal["ToolExecutionFailed"]:
        return "ToolExecutionFailed"


@dataclass(frozen=True)
class ThinkingComplete(BaseEvent):
    """Агрегированный thinking: весь текст рассуждений."""

    content: str

    @classmethod
    def name(cls) -> Literal["ThinkingComplete"]:
        return "ThinkingComplete"


@dataclass(frozen=True)
class AnswerComplete(BaseEvent):
    """Агрегированный ответ: весь текст ответа."""

    content: str

    @classmethod
    def name(cls) -> Literal["AnswerComplete"]:
        return "AnswerComplete"


@dataclass(frozen=True)
class AnswerDiscarded(BaseEvent):
    """Ранее отправленные ``AnswerToken``-ы для этого ``request_id``
    следует отбросить: аккумулированный ``content`` не должен попасть
    ни в итоговое ``AnswerComplete``, ни в ``LLMMessage.content``.

    Эмитится, когда middleware решает переинтерпретировать уже
    проэмиченный текстовый поток как tool call: токены ушли наружу
    ради отзывчивости UI, но в долговременном состоянии (сообщение в
    истории, журнал) текстовой ветки быть не должно — её заменяют
    ``ToolCallBegin`` / ``ToolCallArgumentDelta`` / ``ToolCallComplete``.

    Потребители (``AssistantMessagePersistenceMiddleware``, ``HistorySink``)
    очищают свои answer-буферы для данного ``request_id``; sink'и UI
    могут использовать событие как сигнал «стереть/перезаписать то, что
    только что вывели».
    """

    @classmethod
    def name(cls) -> Literal["AnswerDiscarded"]:
        return "AnswerDiscarded"


@dataclass(frozen=True)
class RefusalComplete(BaseEvent):
    """Агрегированный отказ: весь текст отказа."""

    content: str

    @classmethod
    def name(cls) -> Literal["RefusalComplete"]:
        return "RefusalComplete"


AgentEvent = (
    UserQueryReceived
    | StageStarted
    | StageCompleted
    | GenerationStarted
    | ThinkingStarted
    | ThinkingToken
    | ThinkingComplete
    | AnswerStarted
    | AnswerToken
    | AnswerComplete
    | AnswerDiscarded
    | RefusalToken
    | RefusalComplete
    | GenerationDone
    | GenerationFailed
    | PromptFailed
    | PersistenceFailed
    | ToolCallBegin
    | ToolCallArgumentDelta
    | ToolCallComplete
    | ToolResultReady
    | ToolExecutionFailed
    | UserNoticeReady
)


# Literal-union всех wire-имён событий. Должен содержать ровно те же строки,
# что возвращают ``name()`` подклассов ``BaseEvent``. Синхронность проверяет
# статически функция ``_verify_event_names_exhaustive`` ниже.
AgentEventName: TypeAlias = Literal[
    "UserQueryReceived",
    "StageStarted",
    "StageCompleted",
    "GenerationStarted",
    "ThinkingStarted",
    "ThinkingToken",
    "ThinkingComplete",
    "AnswerStarted",
    "AnswerToken",
    "AnswerComplete",
    "AnswerDiscarded",
    "RefusalToken",
    "RefusalComplete",
    "GenerationDone",
    "GenerationFailed",
    "PromptFailed",
    "PersistenceFailed",
    "ToolCallBegin",
    "ToolCallArgumentDelta",
    "ToolCallComplete",
    "ToolResultReady",
    "ToolExecutionFailed",
    "UserNoticeReady",
]


def _verify_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия: union ``AgentEvent.name()`` == ``EventName``.

    Не вызывается в runtime. pyright проверит, что возвращаемое значение
    ``e.name()`` (union всех Literal-типов) присваивается в ``EventName``.

    При добавлении нового ``AgentEvent`` и забытом имени в ``EventName``
    pyright сообщит: Literal["NewEvent"] is not assignable to EventName.
    """
    return e.name()


# Типы сериализации AgentEvent
AgentEventEncoder = Converter[AgentEvent, str]
AgentEventDecoder = Converter[str, AgentEvent]
AgentEventSerializer = Serializer[AgentEvent, str]
