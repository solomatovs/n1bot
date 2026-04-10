"""Протокол рендерера истории чата по документам."""
from __future__ import annotations

from typing import Any, List, Protocol, runtime_checkable

from domain.doc_chat import DocChatExchange, HistoryBlock


@runtime_checkable
class ChatRenderer(Protocol):
    """Рендерер истории чата."""

    def render_history(self, exchanges: List[DocChatExchange]) -> None: ...
    def render_block(self, block: HistoryBlock) -> None: ...
    def render_streaming(self, placeholder: Any, text: str) -> None: ...
