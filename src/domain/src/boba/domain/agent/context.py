"""Порт сервиса-агрегатора контекста для LLM-вызова."""

from __future__ import annotations

from abc import ABC, abstractmethod

from boba.domain.agent.models import AgentContext, LLMRequest


class ContextService(ABC):
    """Агрегирует всё нужное для одного LLM-вызова.

    Представляет «что мы готовы отправить модели прямо сейчас»: messages,
    tools, модель, sampling params. Терминальный middleware зовёт
    :meth:`build_for_llm` и получает готовый :class:`LLMRequest`, который
    адаптер провайдера мапит в свой API-формат одним ``Converter``-ом.

    Сигнатура query-shaped по смыслу: реализация может быть простой
    агрегацией (``MessageService`` + ``ToolsService`` как есть) или
    retrieval-based (вектор+граф, отбор релевантного среза истории и
    tool-definitions). Терминальный middleware об этом не знает.
    """

    @abstractmethod
    def build_for_llm(self, ctx: AgentContext) -> LLMRequest: ...
