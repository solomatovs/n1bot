"""Протокол рендерера истории чата по документам."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from domain.doc_chat import ChatEvent


@runtime_checkable
class ChatRenderer(Protocol):
    """Рендерер истории чата."""

    def render_event(self, event: ChatEvent) -> None: ...
    def render_streaming(self, placeholder: Any, text: str) -> None: ...
