"""Порт factory, собирающего :class:`LLMRequest` из :class:`AgentContext`."""

from __future__ import annotations

from boba.domain.agent.models import AgentContext, LLMRequest
from boba.domain.core.patterns import ContextFactoryMethod


class LLMRequestFactory(ContextFactoryMethod[AgentContext, LLMRequest]):
    """Доменная специализация :class:`ContextFactoryMethod`:
    финализирует :class:`LLMRequest` из текущего :class:`AgentContext`
    на каждой итерации цикла.

    Реализация может быть простой агрегацией (снимок
    ``ctx.llm_builder`` + ``MessageService`` + ``ToolsService``) или
    retrieval-based (вектор+граф, отбор релевантного среза истории и
    tool-definitions). Терминальный middleware (``OpenAIMiddleware``
    и аналоги) об этом не знает — видит только :meth:`build`.
    """
