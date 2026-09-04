"""Контекст хода чата: общий CallContext плюс поверхность событий фронту.

Ошибки:
RefusalError — вызов вне хода чата либо без id вызова модели.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from boba.identity.context import CallContext, ContextKind, LlmInitiator
from boba.identity.errors import RefusalError

__all__ = ["ChatCallContext", "ChatSurface"]


@runtime_checkable
class ChatSurface(Protocol):
    """Куда ход чата шлёт события фронту: сокет сессии или рассылка треда."""

    async def emit(self, event: str, payload: Mapping[str, Any]) -> bool:
        """True — событие ушло живому слушателю; False — слушать некому."""
        ...


class ChatCallContext(CallContext):
    """Контекст хода чата: вдобавок к общему — поверхность для событий фронту.

    Инструменты, которым нужен чат (панель, карточки, вложения), требуют
    именно его; вне чата они отказывают, а не молчат.
    """

    surface: ChatSurface

    @classmethod
    def require(cls) -> ChatCallContext:
        """Контекст чата; вызов вне чата — RefusalError(CHAT_ONLY)."""
        context = cls.current()
        if not isinstance(context, ChatCallContext):
            got = type(context).__name__
            msg = f"this tool works only inside a chat turn, called from {got}"
            raise RefusalError(ContextKind.CHAT_ONLY, msg)

        return context

    def tool_call_id(self) -> str:
        """id вызова модели: к нему привязываются элементы, созданные инструментом."""
        if not isinstance(self.initiator, LlmInitiator):
            got = type(self.initiator).__name__
            msg = f"tool call id is known only for an llm initiator, got {got}"
            raise RefusalError(ContextKind.NO_TOOL_CALL, msg)

        return self.initiator.tool_call_id
