"""LLM completion service — базовый класс для источников LLM-ответов."""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import LLMRequest
from boba.domain.core.stream import StreamSource


class LLMCompletionService(StreamSource[LLMRequest, AgentEvent]):
    """
    Базовый класс для любого LLM-источника — адаптер или middleware.
    Produce() возвращает поток AgentEvent напрямую.
    """
