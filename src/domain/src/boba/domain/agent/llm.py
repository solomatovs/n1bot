"""LLM completion service — базовый класс для всех слоёв agent middleware."""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext
from boba.domain.core.stream import StreamSource


class LLMMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Базовый класс для любого слоя в middleware-цепочке агента.
    Terminal (адаптер) реализует produce() напрямую.
    Middleware принимает next в __init__ и делегирует ему.
    """
