from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from boba.domain.core.patterns import UuId
from boba.domain.core.tools import Tool
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


@dataclass(frozen=True)
class SamplingParams:
    """Параметры семплирования LLM."""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: list[str] | None = None


@dataclass(frozen=True)
class LLMRequest:
    """Готовый к отправке снимок одной итерации LLM-вызова.

    Агрегирует всё, что нужно чтобы собрать запрос для OpenAI-совместимого
    API: messages, tools, model, sampling params. Собирается
    :class:`LLMRequestFactory` на каждой итерации цикла. Адаптер конкретного
    провайдера мапит ``LLMRequest`` в свою форму в один проход через
    ``Converter``.
    """

    model: str
    messages: list[LLMMessage]
    tools: list[Tool[Any]] = field(default_factory=list)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    tool_choice: str | None = None
    response_format: dict[str, Any] | None = None


@dataclass
class LLMRequestBuilder:
    """Мутабельный накопитель для :class:`LLMRequest`.

    Middleware-стадии заполняют слоты, пересчитываемые **на каждой
    итерации** (system-prompt, tools, sampling…). Слоты с семантикой
    «часть диалога» (user/assistant/tool-сообщения) здесь не живут —
    они идут через :class:`MessageService`, потому что добавляются ровно
    один раз и далее только читаются.

    Участники:
    - ``system_prompt`` — :class:`SystemPromptMiddleware`;
    - ``tools`` — :class:`ToolsDefinitionMiddleware`;
    - ``sampling``/``tool_choice``/``response_format`` — стадии,
      когда появятся.

    :class:`LLMRequestFactory` читает слоты и финализирует в immutable
    :class:`LLMRequest`. Слот в дефолте (``None``, пустой список или
    пустой :class:`SamplingParams`) в итоговый запрос не попадает —
    отключение middleware через DI автоматически убирает соответствующую
    часть.
    """

    system_prompt: str | None = None
    tools: list[Tool[Any]] = field(default_factory=list)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    tool_choice: str | None = None
    response_format: dict[str, Any] | None = None


@dataclass
class AgentContext:
    """
    Мутабельный контекст, передаваемый через стадии Pipeline.
    Стадии читают и дополняют его на каждой итерации цикла.
    """

    request: AgentRequest
    config: AgentConfig
    iteration: int = 0
    llm_builder: LLMRequestBuilder = field(default_factory=LLMRequestBuilder)
