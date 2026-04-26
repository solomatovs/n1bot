"""Middleware-цепочка агента: внутренняя реализация.

Каждый модуль (dialogue.py, error_routing.py, llm.py,
loop_control.py, tools.py) держит middleware-классы своей зоны
ответственности; здесь — только агрегирующий реэкспорт. Публичный API
всего agent-слоя — agent; внешний код должен
импортировать оттуда.

Сам Agent-оркестратор живёт уровнем выше (в
orchestrator) — он не middleware, а
запускающий движок source/sink-цепочки.
"""

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
