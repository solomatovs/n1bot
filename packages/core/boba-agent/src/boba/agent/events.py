"""События агент-слоя (Pydantic v2 discriminated union)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from boba.agent.models import (
    ToolCallFailure,
    ToolCallResult,
)
from boba.llm.events import FinishReason
from boba.llm.models import InvalidToolCall, RequestId, ToolCall
from boba.tools.domain import DefaultTextVisitor


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


# --------------------------------------------------------------------- #
# Base + family ABC
# --------------------------------------------------------------------- #


class BaseAgentEvent(BaseModel, ABC):
    """Базовый класс для всех событий агента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: RequestId


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


class ContentDelta(BaseAgentEvent, ABC):
    """Инкрементальный кусок в «слот» UI."""

    @abstractmethod
    def slot(self) -> SlotKind: ...

    @abstractmethod
    def slot_id(self) -> str: ...

    @abstractmethod
    def chunk(self) -> str: ...


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


# --------------------------------------------------------------------- #
# PhaseTransition events
# --------------------------------------------------------------------- #


class IterationStarted(PhaseTransition):
    """Начало новой итерации агентского цикла."""

    type: Literal["IterationStarted"] = "IterationStarted"
    iteration: int
    max_iterations: int

    def label(self) -> str:
        return f"iteration {self.iteration}/{self.max_iterations}"

    def details(self) -> Mapping[str, str]:
        return {"iteration": str(self.iteration), "max": str(self.max_iterations)}


class LLMRequestSent(PhaseTransition):
    """Round-trip к LLM начат."""

    type: Literal["LLMRequestSent"] = "LLMRequestSent"
    model: str
    messages_count: int
    has_tools: bool
    monotonic_ns: int

    def label(self) -> str:
        tools = " +tools" if self.has_tools else ""
        return f"→ llm: {self.model}, {self.messages_count} msgs{tools}"

    def details(self) -> Mapping[str, str]:
        return {
            "model": self.model,
            "messages_count": str(self.messages_count),
            "has_tools": str(self.has_tools),
        }


class LLMResponseStreamOpened(PhaseTransition):
    """Stream-handle от провайдера получен — парный замер к LLMRequestSent."""

    type: Literal["LLMResponseStreamOpened"] = "LLMResponseStreamOpened"
    monotonic_ns: int

    def label(self) -> str:
        return "← stream open"


class GenerationStarted(PhaseTransition):
    """Первый chunk от LLM — генерация началась."""

    type: Literal["GenerationStarted"] = "GenerationStarted"

    def label(self) -> str:
        return "generation"


class ThinkingStarted(PhaseTransition):
    """Модель начала reasoning."""

    type: Literal["ThinkingStarted"] = "ThinkingStarted"

    def label(self) -> str:
        return "thinking"


class AnswerStarted(PhaseTransition):
    """Модель начала отдавать ответ."""

    type: Literal["AnswerStarted"] = "AnswerStarted"

    def label(self) -> str:
        return "answer"


class ToolCallStreamStarted(PhaseTransition):
    """Tool call объявлен — id и имя пришли, args ещё стримятся."""

    type: Literal["ToolCallStreamStarted"] = "ToolCallStreamStarted"
    index: int
    tool_call_id: str
    tool_name: str

    def label(self) -> str:
        return f"tool#{self.index} stream: {self.tool_name}"

    def details(self) -> Mapping[str, str]:
        return {
            "id": self.tool_call_id,
            "name": self.tool_name,
            "index": str(self.index),
        }


class ToolExecutionStarted(PhaseTransition):
    """Tool готов к исполнению — args разобраны."""

    type: Literal["ToolExecutionStarted"] = "ToolExecutionStarted"
    call: ToolCall

    def label(self) -> str:
        return f"tool exec: {self.call.name}"

    def details(self) -> Mapping[str, str]:
        return {"id": self.call.id, "name": self.call.name}

    def body(self) -> str | None:
        return self.call.args_json()


class GenerationRetried(PhaseTransition):
    """LLM-слой решил повторить запрос. Target = факт retry, не запрос."""

    type: Literal["GenerationRetried"] = "GenerationRetried"
    attempt: int
    reason: str
    status_code: int | None = None

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


class GenerationDone(PhaseTransition):
    """Прогон завершён — пришёл finish_reason."""

    type: Literal["GenerationDone"] = "GenerationDone"
    finish_reason: FinishReason = FinishReason.STOP

    def label(self) -> str:
        return f"generation done ({self.finish_reason.value})"

    def details(self) -> Mapping[str, str]:
        return {"finish_reason": self.finish_reason.value}


# --------------------------------------------------------------------- #
# ContentDelta events
# --------------------------------------------------------------------- #


class ThinkingToken(ContentDelta):
    """Chunk reasoning-токена."""

    type: Literal["ThinkingToken"] = "ThinkingToken"
    token: str

    def slot(self) -> SlotKind:
        return SlotKind.THINKING

    def slot_id(self) -> str:
        return str(self.request_id)

    def chunk(self) -> str:
        return self.token


class AnswerToken(ContentDelta):
    """Chunk текстового ответа для отображения пользователю."""

    type: Literal["AnswerToken"] = "AnswerToken"
    token: str

    def slot(self) -> SlotKind:
        return SlotKind.ANSWER

    def slot_id(self) -> str:
        return str(self.request_id)

    def chunk(self) -> str:
        return self.token


class RefusalToken(ContentDelta):
    """Chunk отказа модели отвечать."""

    type: Literal["RefusalToken"] = "RefusalToken"
    token: str

    def slot(self) -> SlotKind:
        return SlotKind.REFUSAL

    def slot_id(self) -> str:
        return str(self.request_id)

    def chunk(self) -> str:
        return self.token


class ToolCallArgumentDelta(ContentDelta):
    """Chunk аргументов tool call (JSON-строка, может прийти частями)."""

    type: Literal["ToolCallArgumentDelta"] = "ToolCallArgumentDelta"
    index: int
    tool_call_id: str
    tool_name: str
    arguments_chunk: str

    def slot(self) -> SlotKind:
        return SlotKind.TOOL_ARGS

    def slot_id(self) -> str:
        return self.tool_call_id

    def chunk(self) -> str:
        return self.arguments_chunk


# --------------------------------------------------------------------- #
# ContentSnapshot events
# --------------------------------------------------------------------- #


class UserQueryReceived(ContentSnapshot):
    """Запрос пользователя принят агентом и записан в историю."""

    type: Literal["UserQueryReceived"] = "UserQueryReceived"
    query: str

    def slot(self) -> SlotKind:
        return SlotKind.USER_QUERY

    def slot_id(self) -> str:
        return str(self.request_id)

    def body(self) -> str:
        return self.query


class ThinkingComplete(ContentSnapshot):
    """Агрегированный reasoning итерации."""

    type: Literal["ThinkingComplete"] = "ThinkingComplete"
    content: str

    def slot(self) -> SlotKind:
        return SlotKind.THINKING

    def slot_id(self) -> str:
        return str(self.request_id)

    def body(self) -> str:
        return self.content


class AnswerComplete(ContentSnapshot):
    """Агрегированный текстовый ответ итерации (пишется в историю)."""

    type: Literal["AnswerComplete"] = "AnswerComplete"
    content: str

    def slot(self) -> SlotKind:
        return SlotKind.ANSWER

    def slot_id(self) -> str:
        return str(self.request_id)

    def body(self) -> str:
        return self.content


class RefusalComplete(ContentSnapshot):
    """Агрегированный отказ модели."""

    type: Literal["RefusalComplete"] = "RefusalComplete"
    content: str

    def slot(self) -> SlotKind:
        return SlotKind.REFUSAL

    def slot_id(self) -> str:
        return str(self.request_id)

    def body(self) -> str:
        return self.content


class ToolCallComplete(ContentSnapshot):
    """Завершённый tool call (id + имя + args)."""

    type: Literal["ToolCallComplete"] = "ToolCallComplete"
    call: ToolCall

    def slot(self) -> SlotKind:
        return SlotKind.TOOL_CALL

    def slot_id(self) -> str:
        return self.call.id

    def headline(self) -> str:
        return self.call.name

    def body(self) -> str:
        return self.call.args_json()


class ToolResultReady(ContentSnapshot):
    """Результат выполнения tool — вызов и результат."""

    _TEXT_VISITOR: ClassVar[DefaultTextVisitor] = DefaultTextVisitor()

    type: Literal["ToolResultReady"] = "ToolResultReady"
    call: ToolCall
    result: ToolCallResult

    def slot(self) -> SlotKind:
        return SlotKind.TOOL_RESULT

    def slot_id(self) -> str:
        return self.call.id

    def headline(self) -> str:
        return self.call.name

    def body(self) -> str:
        return self.result.result.accept(self._TEXT_VISITOR)


class FeedbackToLLMAdded(ContentSnapshot):
    """Feedback от агента к LLM записан в MessageService."""

    type: Literal["FeedbackToLLMAdded"] = "FeedbackToLLMAdded"
    content: str

    def slot(self) -> SlotKind:
        return SlotKind.FEEDBACK

    def slot_id(self) -> str:
        return str(self.request_id)

    def body(self) -> str:
        return self.content


# --------------------------------------------------------------------- #
# Advisory events
# --------------------------------------------------------------------- #


class InvalidToolCallReceived(Advisory):
    """LLM выдала tool-call с невалидным JSON в args; цикл продолжается."""

    type: Literal["InvalidToolCallReceived"] = "InvalidToolCallReceived"
    invalid: InvalidToolCall

    def headline(self) -> str:
        return f"invalid tool call: {self.invalid.name}"

    def details(self) -> Mapping[str, str]:
        return {
            "id": self.invalid.id,
            "name": self.invalid.name,
        }

    def body(self) -> str | None:
        return f"raw_args: {self.invalid.raw_args}\nerror: {self.invalid.error}"


class ToolExecutionFailed(Advisory):
    """Tool упал — вызов и описание провала; цикл продолжается."""

    type: Literal["ToolExecutionFailed"] = "ToolExecutionFailed"
    call: ToolCall
    failure: ToolCallFailure

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


# --------------------------------------------------------------------- #
# Terminal events
# --------------------------------------------------------------------- #


class GenerationFailed(Terminal):
    """LLM-слой бросил LLMError."""

    type: Literal["GenerationFailed"] = "GenerationFailed"
    error_kind: str
    message: str

    def headline(self) -> str:
        return f"generation failed: {self.error_kind}"

    def details(self) -> Mapping[str, str]:
        return {"kind": self.error_kind}

    def body(self) -> str | None:
        return self.message


class PromptFailed(Terminal):
    """PromptFactory не смогла собрать system-prompt."""

    type: Literal["PromptFailed"] = "PromptFailed"
    error_kind: str
    message: str
    provider: str | None = None

    def headline(self) -> str:
        return f"prompt failed: {self.provider or 'unknown'}"

    def details(self) -> Mapping[str, str]:
        return {
            "kind": self.error_kind,
            "provider": self.provider or "",
        }

    def body(self) -> str | None:
        return self.message


class MaxIterationsReached(Terminal):
    """Цикл агента исчерпал лимит итераций без финального ответа."""

    type: Literal["MaxIterationsReached"] = "MaxIterationsReached"
    error_kind: str
    message: str
    limit: int
    iteration: int

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


class PersistenceFailed(Terminal):
    """Не удалось прочитать/записать journal/хранилище."""

    type: Literal["PersistenceFailed"] = "PersistenceFailed"
    error_kind: str
    message: str

    def headline(self) -> str:
        return f"persistence failed: {self.error_kind}"

    def details(self) -> Mapping[str, str]:
        return {"kind": self.error_kind}

    def body(self) -> str | None:
        return self.message


# --------------------------------------------------------------------- #
# Union + adapter for (de)serialization
# --------------------------------------------------------------------- #


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


AgentEventAdapter: TypeAdapter[AgentEvent] = TypeAdapter(
    Annotated[AgentEvent, Field(discriminator="type")],
)
"""TypeAdapter для (де)сериализации AgentEvent через discriminator='type'.

Использование:
    line: str = AgentEventAdapter.dump_json(event).decode("utf-8")
    event: AgentEvent = AgentEventAdapter.validate_json(line)
"""


def _verify_agent_event_names_exhaustive(e: AgentEvent) -> AgentEventName:
    """Compile-time гарантия синхронности union и Literal-имён."""
    return e.type
