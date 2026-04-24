"""
════════════════════════════════════════════════════════════════════
  События агент-слоя
════════════════════════════════════════════════════════════════════
    BaseAgentEvent (abstract, frozen dataclass)
    │   request_id: RequestId
    │   + classmethod name() -> Literal["..."]
    │
    ├── LifecycleMarker (abstract)        границы фазы, без контента
    │   ├── IterationStarted                iteration, max_iterations
    │   ├── SystemPromptProcessingStarted   content_before
    │   ├── SystemPromptProcessed           content_before/after, duration_ms
    │   ├── UserPromptProcessingStarted     content_before
    │   ├── UserPromptProcessed             content_before/after, duration_ms
    │   ├── LLMUserPromptIssued             user_prompt (snapshot)
    │   ├── LLMRequestStarted               model, messages_count, has_tools, ts
    │   ├── LLMRequestSent                  monotonic_ns (парный)
    │   ├── GenerationStarted
    │   ├── ThinkingStarted
    │   ├── AnswerStarted
    │   ├── ToolCallBegin                   index, tool_call_id, tool_name
    │   ├── ToolExecutionStarted            tool_call_id, tool_name
    │   ├── GenerationRetried               attempt, reason, status_code
    │   └── GenerationDone                  finish_reason
    │
    ├── StreamingDelta (abstract)         инкрементальные куски
    │   ├── ThinkingToken                   token
    │   ├── AnswerToken                     token
    │   ├── RefusalToken                    token
    │   └── ToolCallArgumentDelta           index, arguments
    │
    ├── DurableMessage (abstract)         завершённые message-единицы
    │   ├── UserQueryReceived               query
    │   ├── ThinkingComplete                content
    │   ├── AnswerComplete                  content
    │   ├── RefusalComplete                 content
    │   ├── ToolCallComplete                tool_call_id, tool_name, arguments
    │   ├── ToolResultReady                 tool_call_id, tool_name, content
    │   └── AnswerDiscarded                 (корректирующее)
    │
    ├── UserNotification (abstract)       нотис пользователю, цикл идёт
    │   │   message: str
    │   ├── ToolExecutionFailed             error_kind, tool_call_id, tool_name
    │   └── ToolCallFormatFailed            error_kind
    │
    └── TerminalFailure (abstract)        ошибка, цикл останавливается
        │   error_kind: str
        │   message: str
        ├── GenerationFailed                retryable, status_code
        ├── PromptFailed                    retryable, provider
        ├── MaxIterationsReached            limit, iteration
        └── RepeatedFormatFailure           count, limit
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, TypeAlias

from boba.domain.llm.events import FinishReason
from boba.domain.llm.models import RequestId


@dataclass(frozen=True)
class BaseAgentEvent(ABC):
    """Базовый класс для всех событий агента."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str: ...


# ═════════════════════════════════════════════════════════════════════
#  Семейства
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LifecycleMarker(BaseAgentEvent, ABC):
    """Граница фазы (старт/конец стадии, без контента)."""


@dataclass(frozen=True)
class StreamingDelta(BaseAgentEvent, ABC):
    """Инкрементальный кусок контента между ``*Started`` и ``*Done``."""


@dataclass(frozen=True)
class DurableMessage(BaseAgentEvent, ABC):
    """Завершённое сообщение — идёт в историю/транскрипт.

    History-sink / audit-sink подписывается на семью вместо
    перечисления concrete-типов. Добавление нового concrete'а в
    семью — не требует правок sink'ов, подписанных на семью.
    """


@dataclass(frozen=True)
class UserNotification(BaseAgentEvent, ABC):
    """Нотис пользователю через sink, цикл агента продолжается.

    ``message`` — текст для рендеринга. Concrete-children добавляют
    свои поля (``error_kind``, ``tool_call_id``, ...).

    Отличается от :class:`TerminalFailure` именно тем, что цикл не
    прерывается: LLM на следующей итерации увидит feedback (через
    ``LLMMessage`` в :class:`MessageService`) и может скорректировать
    поведение.
    """

    message: str


@dataclass(frozen=True)
class TerminalFailure(BaseAgentEvent, ABC):
    """Ошибка, останавливающая цикл через
    :class:`~boba.domain.agent.meat.loop_control.StopOnAnyFailure`.
    """

    error_kind: str
    message: str


# ═════════════════════════════════════════════════════════════════════
#  Concrete: DurableMessage
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class UserQueryReceived(DurableMessage):
    """Запрос пользователя принят агентом.

    Эмитится один раз — :class:`~boba.domain.agent.meat.prompt.\
UserPromptMiddleware` на ``ctx.iteration == 1`` после заполнения
    ``ctx.request.user_message``. Запись сообщения в
    :class:`MessageService` делает
    :class:`~boba.domain.agent.meat.llm_invoke.\
UserMessagePersistenceMiddleware` на границе с LLM.
    """

    query: str

    @classmethod
    def name(cls) -> Literal["UserQueryReceived"]:
        return "UserQueryReceived"


@dataclass(frozen=True)
class ThinkingComplete(DurableMessage):
    """Агрегированный reasoning: весь текст рассуждений итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["ThinkingComplete"]:
        return "ThinkingComplete"


@dataclass(frozen=True)
class AnswerComplete(DurableMessage):
    """Агрегированный текстовый ответ итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["AnswerComplete"]:
        return "AnswerComplete"


@dataclass(frozen=True)
class RefusalComplete(DurableMessage):
    """Агрегированный отказ модели."""

    content: str

    @classmethod
    def name(cls) -> Literal["RefusalComplete"]:
        return "RefusalComplete"


@dataclass(frozen=True)
class ToolCallComplete(DurableMessage):
    """Агрегированный tool call: имя + полные сериализованные arguments.

    Эмитится :class:`~boba.domain.agent.meat.dialogue.\
AssistantMessagePersistenceMiddleware` при ``GenerationDone``
    — после накопления ``ToolCallBegin`` + серии
    ``ToolCallArgumentDelta``. :class:`~boba.domain.agent.meat.\
tools.ToolExecutionMiddleware` реагирует на это событие и
    исполняет tool.
    """

    tool_call_id: str
    tool_name: str
    arguments: str

    @classmethod
    def name(cls) -> Literal["ToolCallComplete"]:
        return "ToolCallComplete"


@dataclass(frozen=True)
class ToolResultReady(DurableMessage):
    """Результат успешного выполнения tool.

    Ошибки сюда не попадают — для них
    :class:`ToolExecutionFailed` из семьи :class:`UserNotification`.
    """

    tool_call_id: str
    tool_name: str
    content: str

    @classmethod
    def name(cls) -> Literal["ToolResultReady"]:
        return "ToolResultReady"


@dataclass(frozen=True)
class AnswerDiscarded(DurableMessage):
    """Ранее эмитированные ``AnswerToken``-ы для данного ``request_id``
    следует отбросить: аккумулированный ``content`` не должен попасть
    ни в ``AnswerComplete``, ни в ``LLMMessage.content``.

    Эмитится, когда middleware решает переинтерпретировать уже
    проэмиченный текстовый поток как tool call (strict content-tool-
    call парсер). Токены уже ушли наружу ради отзывчивости UI, но в
    durable-состоянии (история, журнал) текстовой ветки быть не должно
    — её заменяют ``ToolCall*``-события.

    Потребители (``AssistantMessagePersistenceMiddleware``, HistorySink)
    очищают свои answer-буферы для данного ``request_id``; UI-sink
    может использовать событие как «стереть/перезаписать только что
    выведенное».
    """

    @classmethod
    def name(cls) -> Literal["AnswerDiscarded"]:
        return "AnswerDiscarded"


# ═════════════════════════════════════════════════════════════════════
#  Concrete: LifecycleMarker
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class IterationStarted(LifecycleMarker):
    """Начало новой итерации агентского цикла."""

    iteration: int
    max_iterations: int

    @classmethod
    def name(cls) -> Literal["IterationStarted"]:
        return "IterationStarted"


@dataclass(frozen=True)
class SystemPromptProcessingStarted(LifecycleMarker):
    """
    System-prompt middleware начал сборку через :class:`PromptFactory`.
    """

    @classmethod
    def name(cls) -> Literal["SystemPromptProcessingStarted"]:
        return "SystemPromptProcessingStarted"


@dataclass(frozen=True)
class SystemPromptProcessed(LifecycleMarker):
    """System-prompt middleware завершил сборку.

    ``content_after`` — итоговый текст system-промпта, записанный в
    :class:`MessageService` (``None`` если провайдеры вернули пусто и
    в MS ничего не пишется).
    ``duration_ms`` — время работы :class:`PromptFactory`.
    """

    content: str
    duration_ms: float

    @classmethod
    def name(cls) -> Literal["SystemPromptProcessed"]:
        return "SystemPromptProcessed"


@dataclass(frozen=True)
class UserPromptProcessingStarted(LifecycleMarker):
    """User-prompt middleware начал сборку через :class:`PromptFactory`."""

    @classmethod
    def name(cls) -> Literal["UserPromptProcessingStarted"]:
        return "UserPromptProcessingStarted"


@dataclass(frozen=True)
class UserPromptProcessed(LifecycleMarker):
    """User-prompt middleware завершил сборку."""

    content: str
    duration_ms: float

    @classmethod
    def name(cls) -> Literal["UserPromptProcessed"]:
        return "UserPromptProcessed"


@dataclass(frozen=True)
class LLMUserPromptIssued(BaseAgentEvent):
    """Зеркало :class:`~boba.domain.llm.events.LLMUserPromptIssued`.

    Snapshot user-prompt'а, отправленного провайдеру. Эмитится прямо
    перед :class:`LLMRequestStarted` — для observability/audit.
    """

    user_prompt: str

    @classmethod
    def name(cls) -> Literal["LLMUserPromptIssued"]:
        return "LLMUserPromptIssued"


@dataclass(frozen=True)
class LLMRequestStarted(LifecycleMarker):
    """HTTP-запрос к LLM-провайдеру вот-вот будет отправлен.

    Парный к :class:`LLMRequestSent` — даёт замер длительности
    провайдер-вызова через разницу ``monotonic_ns``.
    """

    model: str
    messages_count: int
    has_tools: bool
    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestStarted"]:
        return "LLMRequestStarted"


@dataclass(frozen=True)
class LLMRequestSent(LifecycleMarker):
    """HTTP-запрос к LLM-провайдеру отправлен — стрим-handle получен.

    Парный к :class:`LLMRequestStarted`. Метаданные запроса живут
    на ``Started`` — здесь только закрывающий ``monotonic_ns``.
    """

    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestSent"]:
        return "LLMRequestSent"


@dataclass(frozen=True)
class GenerationStarted(LifecycleMarker):
    """Первый chunk от LLM — генерация началась."""

    @classmethod
    def name(cls) -> Literal["GenerationStarted"]:
        return "GenerationStarted"


@dataclass(frozen=True)
class ThinkingStarted(LifecycleMarker):
    """Модель начала reasoning."""

    @classmethod
    def name(cls) -> Literal["ThinkingStarted"]:
        return "ThinkingStarted"


@dataclass(frozen=True)
class AnswerStarted(LifecycleMarker):
    """Модель начала отдавать ответ."""

    @classmethod
    def name(cls) -> Literal["AnswerStarted"]:
        return "AnswerStarted"


@dataclass(frozen=True)
class ToolCallBegin(LifecycleMarker):
    """Начало tool call — пришли ``tool_call_id`` и имя функции.

    ``index`` — порядковый номер вызова в рамках одной итерации
    (OpenAI позволяет параллельные tool_calls через ``parallel_tool_\
calls=True``). Используется для связывания с последующими
    :class:`ToolCallArgumentDelta` и финальным
    :class:`ToolCallComplete`.
    """

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolCallBegin"]:
        return "ToolCallBegin"


@dataclass(frozen=True)
class ToolExecutionStarted(LifecycleMarker):
    """Tool начал исполняться.

    Эмитится :class:`~boba.domain.agent.meat.tools.\
ToolExecutionMiddleware` после успешного разбора arguments и до
    вызова ``tool.execute``. Закрывает слепое пятно между
    :class:`ToolCallComplete` и :class:`ToolResultReady` для
    медленных tools (fs/http/network) — UI может подсветить
    «running» статус.
    """

    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolExecutionStarted"]:
        return "ToolExecutionStarted"


@dataclass(frozen=True)
class GenerationRetried(LifecycleMarker):
    """LLM-слой решил повторить запрос.

    Зеркалит :class:`~boba.domain.llm.events.LLMRetryAttempt`:
    observability-сигнал для sink'ов (UI/журнал), чтобы отрисовать
    retry-статус. Сам retry выполняется внутри LLM-middleware —
    агент-цикл не прерывается и не делает дополнительной итерации.
    """

    attempt: int
    reason: str
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["GenerationRetried"]:
        return "GenerationRetried"


@dataclass(frozen=True)
class GenerationDone(LifecycleMarker):
    """Прогон завершён — пришёл ``finish_reason``."""

    finish_reason: FinishReason = FinishReason.STOP

    def __post_init__(self) -> None:
        if not isinstance(self.finish_reason, FinishReason):
            object.__setattr__(self, "finish_reason", FinishReason(self.finish_reason))

    @classmethod
    def name(cls) -> Literal["GenerationDone"]:
        return "GenerationDone"


# ═════════════════════════════════════════════════════════════════════
#  Concrete: StreamingDelta
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ThinkingToken(StreamingDelta):
    """Chunk reasoning-токена."""

    token: str

    @classmethod
    def name(cls) -> Literal["ThinkingToken"]:
        return "ThinkingToken"


@dataclass(frozen=True)
class AnswerToken(StreamingDelta):
    """Chunk текстового ответа для отображения пользователю."""

    token: str

    @classmethod
    def name(cls) -> Literal["AnswerToken"]:
        return "AnswerToken"


@dataclass(frozen=True)
class RefusalToken(StreamingDelta):
    """Chunk отказа модели отвечать.

    Зеркалит :class:`~boba.domain.llm.events.LLMRefusalToken`.
    Агрегируется в :class:`RefusalComplete` на этапе
    dialogue-persistence (миграция стадии идёт параллельно).
    """

    token: str

    @classmethod
    def name(cls) -> Literal["RefusalToken"]:
        return "RefusalToken"


@dataclass(frozen=True)
class ToolCallArgumentDelta(StreamingDelta):
    """Chunk аргументов tool call (JSON-строка, может прийти частями).

    ``index`` связывает delta с соответствующим
    :class:`ToolCallBegin`. :class:`~boba.domain.agent.meat.\
dialogue.AssistantMessagePersistenceMiddleware` агрегирует их в
    финальный :class:`ToolCallComplete`.
    """

    index: int
    arguments: str

    @classmethod
    def name(cls) -> Literal["ToolCallArgumentDelta"]:
        return "ToolCallArgumentDelta"


# ═════════════════════════════════════════════════════════════════════
#  Concrete: UserNotification
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolExecutionFailed(UserNotification):
    """Tool упал — sink видит факт ошибки, цикл продолжается.

    Feedback для LLM (``LLMMessage(role="tool")``) параллельно
    пишется в :class:`MessageService` самим
    :class:`~boba.domain.agent.meat.tools.ToolExecutionMiddleware`
    — это отдельный канал, не через events. Событие нужно sink'ам
    (UI/журнал) чтобы отрисовать/залогировать факт ошибки отдельно
    от успешного результата.
    """

    error_kind: str
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolExecutionFailed"]:
        return "ToolExecutionFailed"


@dataclass(frozen=True)
class ToolCallFormatFailed(UserNotification):
    """LLM нарушила формат content-as-JSON tool call'а.

    Цикл продолжается. Feedback для LLM (``LLMMessage(role="user",
    content=<критика>)``) пишется в :class:`MessageService` роутером —
    на следующей итерации LLM увидит своё нарушение и сможет
    переформулировать. Событие — observability для sink'ов.

    Tool call не состоялся (разбор провалился до определения
    ``name``/``tool_call_id``), поэтому полей идентификации вызова
    нет — в отличие от :class:`ToolExecutionFailed`.
    """

    error_kind: str

    @classmethod
    def name(cls) -> Literal["ToolCallFormatFailed"]:
        return "ToolCallFormatFailed"


# ═════════════════════════════════════════════════════════════════════
#  Concrete: TerminalFailure
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class GenerationFailed(TerminalFailure):
    """Терминальный отказ — LLM-слой поднял :class:`LLMError`."""

    retryable: bool = False
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["GenerationFailed"]:
        return "GenerationFailed"


@dataclass(frozen=True)
class MaxIterationsReached(TerminalFailure):
    """Терминальный отказ — агентский цикл исчерпал лимит итераций."""

    limit: int
    iteration: int

    @classmethod
    def name(cls) -> Literal["MaxIterationsReached"]:
        return "MaxIterationsReached"


@dataclass(frozen=True)
class PromptFailed(TerminalFailure):
    """Терминальный отказ — :class:`PromptFactory` не смогла собрать промпт.

    ``provider`` — имя упавшего провайдера (если известно; ``None``
    для ошибок общей логики).
    """

    retryable: bool = False
    provider: str | None = None

    @classmethod
    def name(cls) -> Literal["PromptFailed"]:
        return "PromptFailed"


@dataclass(frozen=True)
class RepeatedFormatFailure(TerminalFailure):
    """Терминальный отказ — модель залипла на неверном формате tool call.

    Эмитится :class:`~boba.domain.agent.meat.tools.\
RepeatedFormatFailureGuardMiddleware` после накопления ``limit``
    подряд :class:`ToolCallFormatFailed` без успешного
    :class:`ToolResultReady` между ними.
    """

    count: int
    limit: int

    @classmethod
    def name(cls) -> Literal["RepeatedFormatFailure"]:
        return "RepeatedFormatFailure"


@dataclass(frozen=True)
class PersistenceFailed(TerminalFailure):
    """Терминальный отказ: не удалось прочитать/записать журнал/хранилище."""

    retryable: bool = False

    @classmethod
    def name(cls) -> Literal["PersistenceFailed"]:
        return "PersistenceFailed"


@dataclass(frozen=True)
class GenericTerminalFailure(TerminalFailure):
    """Fallback для :class:`~boba.domain.core.errors.TerminalError` без
    :class:`~boba.domain.core.errors.UserFeedbackError`.

    Роутер эмитит это событие, чтобы :class:`StopOnAnyFailure`
    корректно остановил цикл при «чистом» ``TerminalError``-маркере
    (без собственного ``to_event``). ``error_kind`` содержит имя
    класса ошибки — sink может фильтровать/логировать по нему.
    """

    @classmethod
    def name(cls) -> Literal["GenericTerminalFailure"]:
        return "GenericTerminalFailure"


# ═════════════════════════════════════════════════════════════════════
#  Union + имена
# ═════════════════════════════════════════════════════════════════════


AgentEvent = (
    UserQueryReceived
    | IterationStarted
    | SystemPromptProcessingStarted
    | SystemPromptProcessed
    | UserPromptProcessingStarted
    | UserPromptProcessed
    | LLMUserPromptIssued
    | LLMRequestStarted
    | LLMRequestSent
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
    | ToolCallBegin
    | ToolCallArgumentDelta
    | ToolCallComplete
    | ToolExecutionStarted
    | ToolResultReady
    | ToolExecutionFailed
    | ToolCallFormatFailed
    | GenerationRetried
    | GenerationDone
    | GenerationFailed
    | PromptFailed
    | MaxIterationsReached
    | RepeatedFormatFailure
    | PersistenceFailed
    | GenericTerminalFailure
)


AgentEventName: TypeAlias = Literal[
    "UserQueryReceived",
    "IterationStarted",
    "SystemPromptProcessingStarted",
    "SystemPromptProcessed",
    "UserPromptProcessingStarted",
    "UserPromptProcessed",
    "LLMUserPromptIssued",
    "LLMRequestStarted",
    "LLMRequestSent",
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
    "ToolCallBegin",
    "ToolCallArgumentDelta",
    "ToolCallComplete",
    "ToolExecutionStarted",
    "ToolResultReady",
    "ToolExecutionFailed",
    "ToolCallFormatFailed",
    "GenerationRetried",
    "GenerationDone",
    "GenerationFailed",
    "PromptFailed",
    "MaxIterationsReached",
    "RepeatedFormatFailure",
    "PersistenceFailed",
    "GenericTerminalFailure",
]


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.name()
