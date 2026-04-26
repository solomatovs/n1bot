"""Агент и middleware-цепочка: внутренняя реализация агент-слоя.

Реэкспортируется через :mod:`boba.domain.agent`.
"""

from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.meat.dialogue import AssistantMessagePersistenceMiddleware
from boba.domain.agent.meat.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.domain.agent.meat.llm import LLMInvokeMiddleware
from boba.domain.agent.meat.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.domain.agent.meat.tools import (
    RepeatedToolCallGuardMiddleware,
    ToolExecutionMiddleware,
)

__all__ = [
    "Agent",
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
