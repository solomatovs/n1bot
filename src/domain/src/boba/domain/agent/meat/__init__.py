"""Middleware-слои AgentLoop.

Пакет разложен по функциональным областям:

- :mod:`.agent` — оркестратор цикла.
- :mod:`.prompt` — сборка system/user промптов.
- :mod:`.dialogue` — синхронизация состояния диалога с
  :class:`MessageService` (live-персистенс + replay истории).
- :mod:`.tools` — определение каталога тулов, исполнение вызовов,
  защита от лупа идентичных вызовов.
- :mod:`.content_tool_call` — эвристики «tool call в ``content``»:
  потоковая и строгая.
- :mod:`.error_routing` — централизованная маршрутизация
  :class:`RoutableError`.
- :mod:`.loop_control` — счётчик итераций и условия остановки цикла.

``__init__`` реэкспортит публичный API ровно в том виде, в каком его
ждут внешние импорты ``from boba.domain.agent.meat import ...``.
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
from boba.domain.agent.meat.dialogue import (
    AssistantMessagePersistenceMiddleware,
    HistoryReplayMiddleware,
)
from boba.domain.agent.meat.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.domain.agent.meat.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.domain.agent.meat.prompt import SystemPromptMiddleware, UserPromptMiddleware
from boba.domain.agent.meat.sampling import SamplingMiddleware
from boba.domain.agent.meat.tools import (
    RepeatedFormatFailureGuardMiddleware,
    RepeatedToolCallGuardMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
)
from boba.domain.agent.models import AgentContext

__all__ = [
    "Agent",
    "AgentContext",
    "AgentErrorRouter",
    "AgentErrorRouterMiddleware",
    "AssistantMessagePersistenceMiddleware",
    "HistoryReplayMiddleware",
    "IterationCounterMiddleware",
    "JsonContentToolCallMiddleware",
    "JsonDepthScanner",
    "JsonHeaderParser",
    "JsonToolCallHeader",
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
