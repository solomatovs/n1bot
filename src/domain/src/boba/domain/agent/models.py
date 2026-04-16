from __future__ import annotations

from dataclasses import dataclass, field

from boba.domain.core.patterns import UuId
from boba.domain.core.workspace import WorkspaceId


class RequestId(UuId):
    """Идентификатор запроса пользователя."""


@dataclass(frozen=True)
class AgentRequest:
    """
    Входные данные для AgentLoop
    Не меняются в процессе выполнения цикла, в отличие от AgentContext
    """

    query: str
    model: str
    workspace_id: WorkspaceId
    request_id: RequestId


@dataclass(frozen=True)
class AgentConfig:
    """
    Настройки AgentLoop
    """

    max_iterations: int = 20
    limit_message: str = "Достигнут лимит итераций агента."


@dataclass
class AgentContext:
    """
    Мутабельный контекст, передаваемый через стадии Pipeline.
    Стадии читают и дополняют его на каждой итерации цикла.
    """

    request: AgentRequest
    config: AgentConfig
    iteration: int = 0


@dataclass(frozen=True)
class LLMToolCall:
    """Готовый tool call."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMMessage:
    """Одно сообщение в истории диалога."""

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
