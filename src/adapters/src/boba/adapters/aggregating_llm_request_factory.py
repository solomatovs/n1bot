"""Тривиальная реализация :class:`LLMRequestFactory` — агрегация без retrieval."""

from __future__ import annotations

from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext, LLMMessage, LLMRequest


class AggregatingLLMRequestFactory(LLMRequestFactory):
    """Финализирует :class:`LLMRequest` из слотов, заполненных middleware'ами.

    Источники:

    - ``ctx.llm_builder.system_prompt`` — :class:`SystemPromptMiddleware`;
    - ``ctx.llm_builder.tools`` — :class:`ToolsDefinitionMiddleware`;
    - ``ctx.llm_builder.sampling/tool_choice/response_format`` —
      соответствующие middleware (когда появятся);
    - :class:`MessageService` — диалог (user/assistant/tool): persistent-
      реализация сама восстанавливает прошлый диалог при создании; новые
      сообщения кладут :class:`UserPromptMiddleware` (user-запрос текущей
      сессии), :class:`AssistantMessagePersistenceMiddleware` (assistant)
      и :class:`ToolExecutionMiddleware` (tool).

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
        messages.extend(self._message_service.message_iter())

        return LLMRequest(
            model=ctx.request.model,
            messages=messages,
            tools=list(b.tools),
            sampling=b.sampling,
            tool_choice=b.tool_choice,
            response_format=b.response_format,
            parallel_tool_calls=b.parallel_tool_calls,
        )
