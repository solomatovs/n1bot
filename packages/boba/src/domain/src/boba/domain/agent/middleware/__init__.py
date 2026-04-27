"""Middleware-цепочка агента: внутренний реэкспорт."""

from boba.domain.agent.middleware.dialogue import AssistantMessagePersistenceMiddleware
from boba.domain.agent.middleware.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.domain.agent.middleware.llm import LLMInvokeMiddleware
from boba.domain.agent.middleware.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.domain.agent.middleware.tools import (
    RepeatedToolCallGuardMiddleware,
    ToolExecutionMiddleware,
)

__all__ = [
    "AgentErrorRouter",
    "AgentErrorRouterMiddleware",
    "AssistantMessagePersistenceMiddleware",
    "IterationCounterMiddleware",
    "LLMInvokeMiddleware",
    "RepeatedToolCallGuardMiddleware",
    "StopOnAnyFailure",
    "StopOnFinished",
    "ToolExecutionMiddleware",
]
