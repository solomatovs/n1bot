"""Рендер истории чата."""
from __future__ import annotations

from typing import List

import streamlit as st

from domain.chat import ChatMessage


def render_chat_history(history: List[ChatMessage]) -> None:
    """Отрисовать историю чата с раскрывающимися блоками контекста и размышлений."""
    for msg in history:
        _render_user_message(msg.question)
        _render_assistant_message(msg)


def _render_user_message(question: str) -> None:
    with st.chat_message("user"):
        st.markdown(question)


def _render_assistant_message(msg: ChatMessage) -> None:
    with st.chat_message("assistant"):
        if msg.rag_context:
            with st.expander("Найденный контекст из базы знаний", expanded=False):
                st.markdown(msg.rag_context)
        if msg.thinking:
            with st.expander("Процесс размышления", expanded=False):
                st.markdown(msg.thinking)
        st.markdown(msg.answer)
