"""События агент-слоя.

::

    BaseAgentEvent (abstract, frozen dataclass)
    │   request_id: RequestId
    │   + classmethod name() -> str
    │
    ├── PhaseTransition (abstract)        граница фазы; могут нести метаданные round-trip
    │   │   label() -> str
    │   │   details() -> Mapping[str, str]
    │   │   body() -> str | None
    │   │   severity() -> Severity (INFO по умолчанию)
    │   ├── IterationStarted                iteration, max_iterations
    │   ├── LLMRequestSent                  model, messages_count, has_tools, monotonic_ns
    │   ├── LLMResponseStreamOpened         monotonic_ns (парный к LLMRequestSent)
    │   ├── GenerationStarted
    │   ├── ThinkingStarted
    │   ├── AnswerStarted
    │   ├── ToolCallStreamStarted           index, tool_call_id, tool_name
    │   ├── ToolExecutionStarted            call: LLMToolCall
    │   ├── GenerationRetried               attempt, reason, status_code (severity=WARN)
    │   └── GenerationDone                  finish_reason
    │
    ├── ContentDelta (abstract)           инкрементальный кусок в слот UI
    │   │   slot() -> SlotKind
    │   │   slot_id() -> str               — ключ слота (rid, tool_call_id, ...)
    │   │   chunk() -> str
    │   ├── ThinkingToken
    │   ├── AnswerToken
    │   ├── RefusalToken
    │   └── ToolCallArgumentDelta           index, tool_call_id, tool_name
    │
    ├── ContentSnapshot (abstract)        завершённое сообщение в диалоге
    │   │   slot() -> SlotKind
    │   │   slot_id() -> str
    │   │   headline() -> str | None
    │   │   body() -> str
    │   ├── UserQueryReceived               query
    │   ├── ThinkingComplete                content
    │   ├── AnswerComplete                  content
    │   ├── RefusalComplete                 content
    │   ├── ToolCallComplete                call: LLMToolCall
    │   ├── ToolResultReady                 call: LLMToolCall, result: ToolCallResult
    │   └── FeedbackToLLMAdded              content
    │
    ├── Advisory (abstract)               нефатальный нотис, цикл идёт
    │   │   headline() -> str
    │   │   details() -> Mapping[str, str]
    │   │   body() -> str | None
    │   │   severity() -> Severity (WARN по умолчанию)
    │   └── ToolExecutionFailed             call, failure
    │
    └── Terminal (abstract)               цикл остановлен
        │   headline() -> str
        │   details() -> Mapping[str, str]
        │   body() -> str | None
        │   severity() -> Severity (ERROR по умолчанию)
        ├── GenerationFailed                error_kind, message
        ├── PromptFailed                    provider, error_kind, message
        ├── MaxIterationsReached            limit, iteration
        └── PersistenceFailed               target, error_kind, message

Sink матчится только по семьям и дёргает интерфейс — concrete-типы ему
видны, но не нужны: добавление нового concrete'а не требует правок sink'ов.

Self-sufficient: каждое событие несёт *только свой target*. Сообщения,
которые шли в LLM, уже были эмитированы как ``ContentSnapshot``-ы; сумма
снапшотов = диалог. Поэтому ни ``LLMRequestSent``, ни ``GenerationFailed``
не дублируют messages-список — у каждого своя ответственность.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias

from boba.domain.agent.payloads import (
    ToolCallFailure,
    ToolCallResult,
)
from boba.domain.llm.events import FinishReason
from boba.domain.llm.models import LLMToolCall, RequestId

# ═════════════════════════════════════════════════════════════════════
#  Базовые классификаторы
# ═════════════════════════════════════════════════════════════════════


class Severity(StrEnum):
    """Уровень события для sink'а — определяет канал и подсветку."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class SlotKind(StrEnum):
    """Идентификатор «слота» в UI/журнале — куда стримить контент.

    Договорной словарь между ``ContentDelta`` / ``ContentSnapshot``
    и sink'ами. Sink, получивший событие с этим
    кодом, знает, в какой UI-элемент его направить (chainlit-Step,
    отдельный message, область thinking и т.п.).
    """

    USER_QUERY = "user_query"
    THINKING = "thinking"
    ANSWER = "answer"
    REFUSAL = "refusal"
    TOOL_ARGS = "tool_args"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FEEDBACK = "feedback"


# ═════════════════════════════════════════════════════════════════════
#  Базовое событие
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BaseAgentEvent(ABC):
    """Базовый класс для всех событий агента."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str: ...


# ═════════════════════════════════════════════════════════════════════
#  Семьи (интерфейсы для sink'ов)
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PhaseTransition(BaseAgentEvent, ABC):
    """Граница фазы в round-trip'е — sink рисует «пульс».

    Может нести метаданные действия (:class:`LLMRequestSent` —
    параметры round-trip'а, :class:`ToolExecutionStarted` — вызов).
    Контент диалога (messages, history) сюда не попадает — он
    объявляется ``ContentSnapshot``-ами.
    """

    @abstractmethod
    def label(self) -> str: ...

    def details(self) -> Mapping[str, str]:
        return {}

    def body(self) -> str | None:
        return None

    def severity(self) -> Severity:
        return Severity.INFO


@dataclass(frozen=True)
class ContentDelta(BaseAgentEvent, ABC):
    """Инкрементальный кусок в «слот» UI.

    Sink стримит ``chunk()`` в слот, идентифицируемый
    ``slot()`` + ``slot_id()``. Аккумуляция — задача sink'а; событие
    ничего не помнит между вызовами.
    """

    @abstractmethod
    def slot(self) -> SlotKind: ...

    @abstractmethod
    def slot_id(self) -> str: ...

    @abstractmethod
    def chunk(self) -> str: ...


@dataclass(frozen=True)
class ContentSnapshot(BaseAgentEvent, ABC):
    """Завершённое сообщение в диалоге.

    Каждый снапшот соответствует ровно одной записи в
    :class:`MessageService` (или эквивалентному «целевому» содержимому,
    если запись не идёт в историю — например, ``ThinkingComplete``).
    Сумма снапшотов за сессию реконструирует диалог.
    """

    @abstractmethod
    def slot(self) -> SlotKind: ...

    @abstractmethod
    def slot_id(self) -> str: ...

    def headline(self) -> str | None:
        return None

    @abstractmethod
    def body(self) -> str: ...


@dataclass(frozen=True)
class Advisory(BaseAgentEvent, ABC):
    """Нефатальный нотис: что-то пошло не так, цикл продолжается.

    Sink выводит в WARN-канал; LLM получает feedback по отдельному
    каналу через :class:`FeedbackToLLMAdded`-снапшот.
    """

    @abstractmethod
    def headline(self) -> str: ...

    def details(self) -> Mapping[str, str]:
        return {}

    def body(self) -> str | None:
        return None

    def severity(self) -> Severity:
        return Severity.WARN


@dataclass(frozen=True)
class Terminal(BaseAgentEvent, ABC):
    """Терминальный отказ: цикл остановлен.

    :class:`StopOnAnyFailure` ловит любого потомка этого класса.
    """

    @abstractmethod
    def headline(self) -> str: ...

    def details(self) -> Mapping[str, str]:
        return {}

    def body(self) -> str | None:
        return None

    def severity(self) -> Severity:
        return Severity.ERROR


# ═════════════════════════════════════════════════════════════════════
#  Concrete: PhaseTransition
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class IterationStarted(PhaseTransition):
    """Начало новой итерации агентского цикла."""

    iteration: int
    max_iterations: int

    @classmethod
    def name(cls) -> Literal["IterationStarted"]:
        return "IterationStarted"

    def label(self) -> str:
        return f"iteration {self.iteration}/{self.max_iterations}"

    def details(self) -> Mapping[str, str]:
        return {"iteration": str(self.iteration), "max": str(self.max_iterations)}


@dataclass(frozen=True)
class LLMRequestSent(PhaseTransition):
    """Round-trip к LLM начат. Несём только метаданные round-trip'а.

    Сообщения, которые ушли, уже были эмитированы как
    :class:`ContentSnapshot`-события на предыдущих шагах (user-query,
    tool-результаты, feedback). Здесь — только то, что описывает сам
    вызов: model, сколько messages в payload, есть ли tools.
    """

    model: str
    messages_count: int
    has_tools: bool
    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestSent"]:
        return "LLMRequestSent"

    def label(self) -> str:
        tools = " +tools" if self.has_tools else ""
        return f"→ llm: {self.model}, {self.messages_count} msgs{tools}"

    def details(self) -> Mapping[str, str]:
        return {
            "model": self.model,
            "messages_count": str(self.messages_count),
            "has_tools": str(self.has_tools),
        }


@dataclass(frozen=True)
class LLMResponseStreamOpened(PhaseTransition):
    """Stream-handle от провайдера получен — парный замер к LLMRequestSent."""

    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMResponseStreamOpened"]:
        return "LLMResponseStreamOpened"

    def label(self) -> str:
        return "← stream open"


@dataclass(frozen=True)
class GenerationStarted(PhaseTransition):
    """Первый chunk от LLM — генерация началась."""

    @classmethod
    def name(cls) -> Literal["GenerationStarted"]:
        return "GenerationStarted"

    def label(self) -> str:
        return "generation"


@dataclass(frozen=True)
class ThinkingStarted(PhaseTransition):
    """Модель начала reasoning."""

    @classmethod
    def name(cls) -> Literal["ThinkingStarted"]:
        return "ThinkingStarted"

    def label(self) -> str:
        return "thinking"


@dataclass(frozen=True)
class AnswerStarted(PhaseTransition):
    """Модель начала отдавать ответ."""

    @classmethod
    def name(cls) -> Literal["AnswerStarted"]:
        return "AnswerStarted"

    def label(self) -> str:
        return "answer"


@dataclass(frozen=True)
class ToolCallStreamStarted(PhaseTransition):
    """Tool call объявлен — id и имя пришли, args ещё стримятся.

    ``index`` — порядковый номер вызова в рамках одной итерации
    (OpenAI parallel_tool_calls). Полный вызов с args будет в
    :class:`ToolCallComplete`.
    """

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["ToolCallStreamStarted"]:
        return "ToolCallStreamStarted"

    def label(self) -> str:
        return f"tool#{self.index} stream: {self.tool_name}"

    def details(self) -> Mapping[str, str]:
        return {
            "id": self.tool_call_id,
            "name": self.tool_name,
            "index": str(self.index),
        }


@dataclass(frozen=True)
class ToolExecutionStarted(PhaseTransition):
    """Tool готов к исполнению — args разобраны, диспетчер сейчас стартует.

    Несём полный :class:`LLMToolCall`: sink, видя одно это событие,
    знает, что именно сейчас исполняется (id, name, args).
    """

    call: LLMToolCall

    @classmethod
    def name(cls) -> Literal["ToolExecutionStarted"]:
        return "ToolExecutionStarted"

    def label(self) -> str:
        return f"tool exec: {self.call.name}"

    def details(self) -> Mapping[str, str]:
        return {"id": self.call.id, "name": self.call.name}

    def body(self) -> str | None:
        return self.call.arguments


@dataclass(frozen=True)
class GenerationRetried(PhaseTransition):
    """LLM-слой решил повторить запрос. Target = факт retry, не запрос."""

    attempt: int
    reason: str
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["GenerationRetried"]:
        return "GenerationRetried"

    def label(self) -> str:
        return f"retry #{self.attempt}: {self.reason}"

    def details(self) -> Mapping[str, str]:
        return {
            "attempt": str(self.attempt),
            "reason": self.reason,
            "status_code": str(self.status_code)
            if self.status_code is not None
            else "",
        }

    def severity(self) -> Severity:
        return Severity.WARN


@dataclass(frozen=True)
class GenerationDone(PhaseTransition):
    """Прогон завершён — пришёл ``finish_reason``."""

    finish_reason: FinishReason = FinishReason.STOP

    def __post_init__(self) -> None:
        if not isinstance(self.finish_reason, FinishReason):
            object.__setattr__(self, "finish_reason", FinishReason(self.finish_reason))

    @classmethod
    def name(cls) -> Literal["GenerationDone"]:
        return "GenerationDone"

    def label(self) -> str:
        return f"generation done ({self.finish_reason.value})"

    def details(self) -> Mapping[str, str]:
        return {"finish_reason": self.finish_reason.value}


# ═════════════════════════════════════════════════════════════════════
#  Concrete: ContentDelta
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ThinkingToken(ContentDelta):
    """Chunk reasoning-токена."""

    token: str

    @classmethod
    def name(cls) -> Literal["ThinkingToken"]:
        return "ThinkingToken"

    def slot(self) -> SlotKind:
        return SlotKind.THINKING

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def chunk(self) -> str:
        return self.token


@dataclass(frozen=True)
class AnswerToken(ContentDelta):
    """Chunk текстового ответа для отображения пользователю."""

    token: str

    @classmethod
    def name(cls) -> Literal["AnswerToken"]:
        return "AnswerToken"

    def slot(self) -> SlotKind:
        return SlotKind.ANSWER

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def chunk(self) -> str:
        return self.token


@dataclass(frozen=True)
class RefusalToken(ContentDelta):
    """Chunk отказа модели отвечать."""

    token: str

    @classmethod
    def name(cls) -> Literal["RefusalToken"]:
        return "RefusalToken"

    def slot(self) -> SlotKind:
        return SlotKind.REFUSAL

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def chunk(self) -> str:
        return self.token


@dataclass(frozen=True)
class ToolCallArgumentDelta(ContentDelta):
    """Chunk аргументов tool call (JSON-строка, может прийти частями).

    Несём ``tool_name`` помимо id — sink не должен искать имя
    в прошлых событиях, чтобы отрисовать «куда» льются args.
    """

    index: int
    tool_call_id: str
    tool_name: str
    arguments_chunk: str

    @classmethod
    def name(cls) -> Literal["ToolCallArgumentDelta"]:
        return "ToolCallArgumentDelta"

    def slot(self) -> SlotKind:
        return SlotKind.TOOL_ARGS

    def slot_id(self) -> str:
        return self.tool_call_id

    def chunk(self) -> str:
        return self.arguments_chunk


# ═════════════════════════════════════════════════════════════════════
#  Concrete: ContentSnapshot
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class UserQueryReceived(ContentSnapshot):
    """Запрос пользователя принят агентом и записан в историю."""

    query: str

    @classmethod
    def name(cls) -> Literal["UserQueryReceived"]:
        return "UserQueryReceived"

    def slot(self) -> SlotKind:
        return SlotKind.USER_QUERY

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def body(self) -> str:
        return self.query


@dataclass(frozen=True)
class ThinkingComplete(ContentSnapshot):
    """Агрегированный reasoning итерации."""

    content: str

    @classmethod
    def name(cls) -> Literal["ThinkingComplete"]:
        return "ThinkingComplete"

    def slot(self) -> SlotKind:
        return SlotKind.THINKING

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def body(self) -> str:
        return self.content


@dataclass(frozen=True)
class AnswerComplete(ContentSnapshot):
    """Агрегированный текстовый ответ итерации (пишется в историю)."""

    content: str

    @classmethod
    def name(cls) -> Literal["AnswerComplete"]:
        return "AnswerComplete"

    def slot(self) -> SlotKind:
        return SlotKind.ANSWER

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def body(self) -> str:
        return self.content


@dataclass(frozen=True)
class RefusalComplete(ContentSnapshot):
    """Агрегированный отказ модели."""

    content: str

    @classmethod
    def name(cls) -> Literal["RefusalComplete"]:
        return "RefusalComplete"

    def slot(self) -> SlotKind:
        return SlotKind.REFUSAL

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def body(self) -> str:
        return self.content


@dataclass(frozen=True)
class ToolCallComplete(ContentSnapshot):
    """Завершённый tool call: id + имя + полные args (как часть assistant-сообщения).

    ``ToolExecutionMiddleware`` слушает это событие и исполняет tool.
    """

    call: LLMToolCall

    @classmethod
    def name(cls) -> Literal["ToolCallComplete"]:
        return "ToolCallComplete"

    def slot(self) -> SlotKind:
        return SlotKind.TOOL_CALL

    def slot_id(self) -> str:
        return self.call.id

    def headline(self) -> str:
        return self.call.name

    def body(self) -> str:
        return self.call.arguments


@dataclass(frozen=True)
class ToolResultReady(ContentSnapshot):
    """Результат успешного выполнения tool — несём И вызов, И результат.

    Self-sufficient: sink, видя только это событие, знает name+args
    исходного вызова и текст результата. Парный к
    :class:`ToolExecutionFailed` (один и тот же вызов, разный исход).
    """

    call: LLMToolCall
    result: ToolCallResult

    @classmethod
    def name(cls) -> Literal["ToolResultReady"]:
        return "ToolResultReady"

    def slot(self) -> SlotKind:
        return SlotKind.TOOL_RESULT

    def slot_id(self) -> str:
        return self.call.id

    def headline(self) -> str:
        return self.call.name

    def body(self) -> str:
        return self.result.content


@dataclass(frozen=True)
class FeedbackToLLMAdded(ContentSnapshot):
    """Feedback от агента к LLM записан в ``MessageService``.

    Эмитится middleware-роутером ошибок и guard'ами луппинга — каждое
    сообщение, попадающее в историю, должно иметь парный снапшот.
    Без этого инвариант «снапшоты = диалог» нарушается.
    """

    content: str

    @classmethod
    def name(cls) -> Literal["FeedbackToLLMAdded"]:
        return "FeedbackToLLMAdded"

    def slot(self) -> SlotKind:
        return SlotKind.FEEDBACK

    def slot_id(self) -> str:
        return self.request_id.to_wire()

    def body(self) -> str:
        return self.content


# ═════════════════════════════════════════════════════════════════════
#  Concrete: Advisory
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolExecutionFailed(Advisory):
    """Tool упал — несём И вызов, И описание провала. Цикл продолжается.

    Парный к :class:`ToolResultReady`. Запись ``role="tool"`` с текстом
    ошибки делает middleware параллельно — для LLM на следующей итерации.
    """

    call: LLMToolCall
    failure: ToolCallFailure

    @classmethod
    def name(cls) -> Literal["ToolExecutionFailed"]:
        return "ToolExecutionFailed"

    def headline(self) -> str:
        return f"tool failed: {self.call.name}"

    def details(self) -> Mapping[str, str]:
        return {
            "id": self.call.id,
            "name": self.call.name,
            "kind": self.failure.error_kind,
        }

    def body(self) -> str | None:
        return f"args: {self.call.arguments}\nerror: {self.failure.message}"


# ═════════════════════════════════════════════════════════════════════
#  Concrete: Terminal
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class GenerationFailed(Terminal):
    """LLM-слой бросил :class:`LLMError`."""

    error_kind: str
    message: str

    @classmethod
    def name(cls) -> Literal["GenerationFailed"]:
        return "GenerationFailed"

    def headline(self) -> str:
        return f"generation failed: {self.error_kind}"

    def details(self) -> Mapping[str, str]:
        return {"kind": self.error_kind}

    def body(self) -> str | None:
        return self.message


@dataclass(frozen=True)
class PromptFailed(Terminal):
    """:class:`PromptFactory` не смогла собрать system-prompt.

    ``provider`` — имя упавшего провайдера, если известно; ``None`` для
    ошибок общей логики.
    """

    error_kind: str
    message: str
    provider: str | None = None

    @classmethod
    def name(cls) -> Literal["PromptFailed"]:
        return "PromptFailed"

    def headline(self) -> str:
        return f"prompt failed: {self.provider or 'unknown'}"

    def details(self) -> Mapping[str, str]:
        return {
            "kind": self.error_kind,
            "provider": self.provider or "",
        }

    def body(self) -> str | None:
        return self.message


@dataclass(frozen=True)
class MaxIterationsReached(Terminal):
    """Цикл агента исчерпал лимит итераций без финального ответа."""

    error_kind: str
    message: str
    limit: int
    iteration: int

    @classmethod
    def name(cls) -> Literal["MaxIterationsReached"]:
        return "MaxIterationsReached"

    def headline(self) -> str:
        return f"max iterations: {self.iteration}/{self.limit}"

    def details(self) -> Mapping[str, str]:
        return {
            "kind": self.error_kind,
            "limit": str(self.limit),
            "iteration": str(self.iteration),
        }

    def body(self) -> str | None:
        return self.message


@dataclass(frozen=True)
class PersistenceFailed(Terminal):
    """Не удалось прочитать/записать journal/хранилище."""

    error_kind: str
    message: str

    @classmethod
    def name(cls) -> Literal["PersistenceFailed"]:
        return "PersistenceFailed"

    def headline(self) -> str:
        return f"persistence failed: {self.error_kind}"

    def details(self) -> Mapping[str, str]:
        return {"kind": self.error_kind}

    def body(self) -> str | None:
        return self.message


# ═════════════════════════════════════════════════════════════════════
#  Union + имена (compile-time exhaustiveness)
# ═════════════════════════════════════════════════════════════════════


AgentEvent = (
    # PhaseTransition
    IterationStarted
    | LLMRequestSent
    | LLMResponseStreamOpened
    | GenerationStarted
    | ThinkingStarted
    | AnswerStarted
    | ToolCallStreamStarted
    | ToolExecutionStarted
    | GenerationRetried
    | GenerationDone
    # ContentDelta
    | ThinkingToken
    | AnswerToken
    | RefusalToken
    | ToolCallArgumentDelta
    # ContentSnapshot
    | UserQueryReceived
    | ThinkingComplete
    | AnswerComplete
    | RefusalComplete
    | ToolCallComplete
    | ToolResultReady
    | FeedbackToLLMAdded
    # Advisory
    | ToolExecutionFailed
    # Terminal
    | GenerationFailed
    | PromptFailed
    | MaxIterationsReached
    | PersistenceFailed
)


AgentEventName: TypeAlias = Literal[
    "IterationStarted",
    "LLMRequestSent",
    "LLMResponseStreamOpened",
    "GenerationStarted",
    "ThinkingStarted",
    "AnswerStarted",
    "ToolCallStreamStarted",
    "ToolExecutionStarted",
    "GenerationRetried",
    "GenerationDone",
    "ThinkingToken",
    "AnswerToken",
    "RefusalToken",
    "ToolCallArgumentDelta",
    "UserQueryReceived",
    "ThinkingComplete",
    "AnswerComplete",
    "RefusalComplete",
    "ToolCallComplete",
    "ToolResultReady",
    "FeedbackToLLMAdded",
    "ToolExecutionFailed",
    "GenerationFailed",
    "PromptFailed",
    "MaxIterationsReached",
    "PersistenceFailed",
]


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.name()
