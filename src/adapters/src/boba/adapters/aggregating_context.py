"""Тривиальная реализация :class:`ContextService` — агрегация без retrieval."""

from __future__ import annotations

from boba.domain.agent.context import ContextService
from boba.domain.agent.models import AgentContext, LLMRequest
from boba.domain.core.messages import MessageService
from boba.domain.core.tools import ToolsService


class AggregatingContextService(ContextService):
    """Берёт текущий снимок ``MessageService`` и каталог ``ToolsService``,
    клеит в :class:`LLMRequest`. Никаких retrieval — всё, что накопилось.
    """

    def __init__(
        self,
        message_service: MessageService,
        tools_service: ToolsService,
    ) -> None:
        self._message_service = message_service
        self._tools_service = tools_service

    def build_for_llm(self, ctx: AgentContext) -> LLMRequest:
        return LLMRequest(
            model=ctx.request.model,
            messages=list(self._message_service.message_iter()),
            tools=list(self._tools_service.tools()),
        )
