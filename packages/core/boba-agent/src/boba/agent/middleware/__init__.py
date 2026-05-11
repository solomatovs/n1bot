"""Middleware-цепочка агента: внутренний реэкспорт."""

from boba.agent.middleware.dialogue import AssistantMessagePersistenceMiddleware
from boba.agent.middleware.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.agent.middleware.llm import LLMInvokeMiddleware
from boba.agent.middleware.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.agent.middleware.tools import (
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
