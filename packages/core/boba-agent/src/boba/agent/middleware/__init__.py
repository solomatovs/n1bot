"""Middleware-цепочка агента: внутренний реэкспорт."""

from boba.agent.middleware.assistant_snapshot import AssistantSnapshotMiddleware
from boba.agent.middleware.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.agent.middleware.history import HistoryRecorderMiddleware
from boba.agent.middleware.llm import LLMInvokeMiddleware
from boba.agent.middleware.loop_control import (
    IterationCounterConfig,
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.agent.middleware.stamper import EventStamperMiddleware
from boba.agent.middleware.tools import (
    RepeatedToolCallGuardMiddleware,
    ToolExecutionMiddleware,
)

__all__ = [
    "AgentErrorRouter",
    "AgentErrorRouterMiddleware",
    "AssistantSnapshotMiddleware",
    "EventStamperMiddleware",
    "HistoryRecorderMiddleware",
    "IterationCounterConfig",
    "IterationCounterMiddleware",
    "LLMInvokeMiddleware",
    "RepeatedToolCallGuardMiddleware",
    "StopOnAnyFailure",
    "StopOnFinished",
    "ToolExecutionMiddleware",
]
