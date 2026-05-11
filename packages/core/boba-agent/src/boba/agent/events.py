"""События агент-слоя."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias

from boba.agent.models import (
    ToolCallFailure,
    ToolCallResult,
)
from boba.llm.events import FinishReason
from boba.llm.models import InvalidToolCall, RequestId, ToolCall


class Severity(StrEnum):
    """Уровень события для sink'а."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class SlotKind(StrEnum):
    """Идентификатор «слота» в UI/журнале — куда стримить контент."""

    USER_QUERY = "user_query"
    THINKING = "thinking"
    ANSWER = "answer"
    REFUSAL = "refusal"
    TOOL_ARGS = "tool_args"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FEEDBACK = "feedback"

@dataclass(frozen=True)
class BaseAgentEvent(ABC):
    """Базовый класс для всех событий агента."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str: ...

@dataclass(frozen=True)
class PhaseTransition(BaseAgentEvent, ABC):
    """Граница фазы в round-trip'е."""

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
    """Инкрементальный кусок в «слот» UI."""

    @abstractmethod
    def slot(self) -> SlotKind: ...

    @abstractmethod
    def slot_id(self) -> str: ...

    @abstractmethod
    def chunk(self) -> str: ...


@dataclass(frozen=True)
class ContentSnapshot(BaseAgentEvent, ABC):
    """Завершённое сообщение в диалоге."""

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
    """Нефатальный нотис: цикл продолжается."""

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
    """Терминальный отказ: цикл остановлен."""

    @abstractmethod
    def headline(self) -> str: ...

    def details(self) -> Mapping[str, str]:
        return {}

    def body(self) -> str | None:
        return None

    def severity(self) -> Severity:
        return Severity.ERROR

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
    """Round-trip к LLM начат."""

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
    """Tool call объявлен — id и имя пришли, args ещё стримятся."""

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
    """Tool готов к исполнению — args разобраны."""

    call: ToolCall

    @classmethod
    def name(cls) -> Literal["ToolExecutionStarted"]:
        return "ToolExecutionStarted"

    def label(self) -> str:
        return f"tool exec: {self.call.name}"

    def details(self) -> Mapping[str, str]:
        return {"id": self.call.id, "name": self.call.name}

    def body(self) -> str | None:
        return self.call.args_json()


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
    """Прогон завершён — пришёл finish_reason."""

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
    """Chunk аргументов tool call (JSON-строка, может прийти частями)."""

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
class InvalidToolCallReceived(Advisory):
    """LLM выдала tool-call с невалидным JSON в args; цикл продолжается."""

    invalid: InvalidToolCall

    @classmethod
    def name(cls) -> Literal["InvalidToolCallReceived"]:
        return "InvalidToolCallReceived"

    def headline(self) -> str:
        return f"invalid tool call: {self.invalid.name}"

    def details(self) -> Mapping[str, str]:
        return {
            "id": self.invalid.id,
            "name": self.invalid.name,
        }

    def body(self) -> str | None:
        return f"raw_args: {self.invalid.raw_args}\nerror: {self.invalid.error}"


@dataclass(frozen=True)
class ToolCallComplete(ContentSnapshot):
    """Завершённый tool call (id + имя + args)."""

    call: ToolCall

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
        return self.call.args_json()


@dataclass(frozen=True)
class ToolResultReady(ContentSnapshot):
    """Результат выполнения tool — вызов и результат."""

    call: ToolCall
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
    """Feedback от агента к LLM записан в MessageService."""

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

@dataclass(frozen=True)
class ToolExecutionFailed(Advisory):
    """Tool упал — вызов и описание провала; цикл продолжается."""

    call: ToolCall
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
        return f"args: {self.call.args_json()}\nerror: {self.failure.message}"

@dataclass(frozen=True)
class GenerationFailed(Terminal):
    """LLM-слой бросил LLMError."""

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
    """PromptFactory не смогла собрать system-prompt."""

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
    | InvalidToolCallReceived
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
    "InvalidToolCallReceived",
    "GenerationFailed",
    "PromptFailed",
    "MaxIterationsReached",
    "PersistenceFailed",
]


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.name()
