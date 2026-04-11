"""Простой рендерер — отображает историю чата как есть.

Каждое событие рендерится по типу:
    USER → chat_message("user")
    collapsible (search/context/thinking) → chat_message("assistant") + expander
    ASSISTANT → chat_message("assistant")
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from domain.doc_chat import ChatEvent


class SimpleChatRenderer:
    """Рендер: плоский поток событий, каждое форматируется по типу."""

    def render_event(self, event: ChatEvent) -> None:
        """Отрисовать одно событие с нужным chat-пузырём и форматированием."""
        role = "user" if event.is_user else "assistant"
        with st.chat_message(role):
            if event.event_type.collapsible:
                with st.expander(event.event_type.label):
                    st.markdown(event.content)
            else:
                st.markdown(event.content)

    def render_streaming(self, placeholder: Any, text: str) -> None:
        """Обновить placeholder стримящимся текстом."""
        if text.strip():
            placeholder.markdown(text + "▌")
