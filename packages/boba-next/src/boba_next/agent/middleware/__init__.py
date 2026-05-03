"""Middleware-цепочка агента: внутренний реэкспорт."""

from boba_next.agent.middleware.dialogue import AssistantMessagePersistenceMiddleware
from boba_next.agent.middleware.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba_next.agent.middleware.llm import LLMInvokeMiddleware
from boba_next.agent.middleware.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba_next.agent.middleware.tools import (
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
