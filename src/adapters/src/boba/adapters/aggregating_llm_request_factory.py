"""Тривиальная реализация :class:`LLMRequestFactory` — агрегация без retrieval."""

from __future__ import annotations

from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.models import AgentContext, LLMMessage, LLMRequest
from boba.domain.core.messages import MessageService


class AggregatingLLMRequestFactory(LLMRequestFactory):
    """Финализирует :class:`LLMRequest` из слотов, заполненных middleware'ами.

    Источники:

    - ``ctx.llm_builder.system_prompt`` — :class:`SystemPromptMiddleware`;
    - ``ctx.llm_builder.user_prompt`` — :class:`UserPromptMiddleware`;
    - ``ctx.llm_builder.tools`` — :class:`ToolsDefinitionMiddleware`;
    - ``ctx.llm_builder.sampling/tool_choice/response_format`` —
      соответствующие middleware (когда появятся);
    - :class:`MessageService` — динамический диалог (assistant/tool).

    Сама фабрика ничего не строит — только читает слоты и склеивает
    финальный immutable :class:`LLMRequest`. Отключение любого middleware
    в DI автоматически убирает соответствующую часть из запроса.
    """

    def __init__(self, message_service: MessageService) -> None:
        self._message_service = message_service

    def build(self, ctx: AgentContext) -> LLMRequest:
        b = ctx.llm_builder
        messages: list[LLMMessage] = []

        if b.system_prompt:
            messages.append(LLMMessage(role="system", content=b.system_prompt))
        if b.user_prompt:
            messages.append(LLMMessage(role="user", content=b.user_prompt))
        messages.extend(self._message_service.message_iter())

        return LLMRequest(
            model=ctx.request.model,
            messages=messages,
            tools=list(b.tools),
            sampling=b.sampling,
            tool_choice=b.tool_choice,
            response_format=b.response_format,
        )
