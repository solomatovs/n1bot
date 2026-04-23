from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boba.domain.core.patterns import UuId
from boba.domain.core.tools.tool import Tool
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
    max_consecutive_tool_calls: int = 3
    max_consecutive_format_failures: int = 3


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


@dataclass
class SamplingParams:
    """Параметры семплирования LLM"""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: list[str] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


@dataclass(frozen=True)
class LLMRequestDefaults:
    """Дефолтные параметры LLM-запроса, применяемые
    :class:`SamplingMiddleware` на каждой итерации. Живут в agent-слое
    (аналогично :class:`AgentConfig`), загружаются
    :class:`~boba.infra.config.ConfigLoader` отдельно от
    :class:`~boba.domain.config.LLMConfig` — чтобы транспортный
    :class:`LLMConfig` не тянул семантику запроса.
    """

    sampling: SamplingParams = field(default_factory=SamplingParams)
    parallel_tool_calls: bool | None = None


@dataclass
class LLMToolRequestBuilder:
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None
    tools: list[Tool[Any]] = field(default_factory=list)


@dataclass
class LLMRequestBuilder:
    """
    Класс из которого будет построено одно сообщение в llm

    Middleware-стадии заполняют части этого класса
    А конвертер в конкретную спецификацию api отправляет итоговое сообщение
    """

    # модель для отправки
    model: str | None = None
    # системный prompt
    system_prompt: str | None = None
    # пользовательский prompt
    user_prompt: str | None = None
    # история сообщений существовавшая до текущего запроса
    messages: list[str] = field(default_factory=list)
    # tools builder
    tools: LLMToolRequestBuilder = field(default_factory=LLMToolRequestBuilder)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: dict[str, Any] | None = None


@dataclass
class AgentContext:
    """
    Мутабельный контекст, передаваемый через стадии Pipeline.
    Стадии читают и дополняют его на каждой итерации цикла.
    """

    agent_request: AgentRequest
    config: AgentConfig
    iteration: int = 0
    llm_request: LLMRequestBuilder = field(default_factory=LLMRequestBuilder)
