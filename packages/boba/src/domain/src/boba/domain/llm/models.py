from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from boba.domain.core.patterns import UuId

LLMRole = Literal["system", "user", "assistant", "tool"]


class RequestId(UuId):
    """
    Идентификатор запроса пользователя, проходящий через всю систему.
    """


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMMessage:
    role: LLMRole
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


@dataclass(frozen=True)
class SamplingParams:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: tuple[str, ...] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


@dataclass(frozen=True)
class LLMToolSchema:
    """Декларация тула для LLM-провайдера: имя, описание, JSON-schema.

    Чистый DTO без связи с доменным ``Tool`` (последний живёт в
    :mod:`boba.domain.core.tools` и несёт execute-логику, валидаторы,
    типизированные args). LLM-слой работает только с этим типом —
    каждый адаптер (OpenAI/Anthropic/...) мапит его в свой нативный
    формат.

    ``parameters_schema`` — JSON-Schema-объект (как правило
    ``{"type": "object", "properties": {...}, "required": [...]}``).
    Конверсия ``Tool → LLMToolSchema`` — задача агентского слоя
    (см. :class:`~boba.domain.agent.turn.reducers.ToolsReducer`).
    """

    name: str
    description: str
    parameters_schema: Mapping[str, Any]


@dataclass(frozen=True)
class LLMToolRequest:
    tools: tuple[LLMToolSchema, ...] = ()
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system_message: LLMMessage
    messages: tuple[LLMMessage, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None

    def messages_count(self) -> int:
        """Всего сообщений в запросе"""
        return len(self.messages)

    def has_tools(self) -> bool:
        return bool(self.tools.tools)


@dataclass
class LLMContext:
    request: LLMRequest
    request_id: RequestId
    attempt: int = 0
