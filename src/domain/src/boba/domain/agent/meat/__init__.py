"""Агент и middleware-цепочка: внутренняя реализация агент-слоя.

Реэкспортируется через :mod:`boba.domain.agent`.
"""

from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.meat.content_tool_call import (
    JsonContentToolCallMiddleware,
    JsonDepthScanner,
    JsonHeaderParser,
    JsonToolCallHeader,
    ParsedJsonToolCall,
    StrictJsonContentToolCallMiddleware,
    StrictJsonToolCallParser,
)
from boba.domain.agent.meat.dialogue import AssistantMessagePersistenceMiddleware
from boba.domain.agent.meat.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.domain.agent.meat.history import HistoryMiddleware
from boba.domain.agent.meat.llm import (
    LLMEventToAgentEventConverter,
    LLMInvokeMiddleware,
    NewLLMRequestMiddleware,
)
from boba.domain.agent.meat.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.domain.agent.meat.prompt import (
    SystemPromptMiddleware,
    UserPromptMiddleware,
)
from boba.domain.agent.meat.sampling import SamplingMiddleware
from boba.domain.agent.meat.tools import (
    RepeatedFormatFailureGuardMiddleware,
    RepeatedToolCallGuardMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
)

__all__ = [
    "Agent",
    "AgentErrorRouter",
    "AgentErrorRouterMiddleware",
    "AssistantMessagePersistenceMiddleware",
    "HistoryMiddleware",
    "IterationCounterMiddleware",
    "JsonContentToolCallMiddleware",
    "JsonDepthScanner",
    "JsonHeaderParser",
    "JsonToolCallHeader",
    "LLMEventToAgentEventConverter",
    "LLMInvokeMiddleware",
    "NewLLMRequestMiddleware",
    "ParsedJsonToolCall",
    "RepeatedFormatFailureGuardMiddleware",
    "RepeatedToolCallGuardMiddleware",
    "SamplingMiddleware",
    "StopOnAnyFailure",
    "StopOnFinished",
    "StrictJsonContentToolCallMiddleware",
    "StrictJsonToolCallParser",
    "SystemPromptMiddleware",
    "ToolExecutionMiddleware",
    "ToolsDefinitionMiddleware",
    "UserPromptMiddleware",
]
