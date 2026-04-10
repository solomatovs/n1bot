"""Простой рендерер — отображает историю чата как есть."""
from __future__ import annotations

from typing import Any, List

import streamlit as st

from domain.doc_chat import BlockType, DocChatExchange, HistoryBlock


class SimpleChatRenderer:
    """Рендер: chat-пузыри, контент как есть через st.markdown()."""

    def render_history(self, exchanges: List[DocChatExchange]) -> None:
        for exchange in exchanges:
            user_blocks = [b for b in exchange.blocks if b.block_type == BlockType.USER]
            other_blocks = [b for b in exchange.blocks if b.block_type != BlockType.USER]

            if user_blocks:
                with st.chat_message("user"):
                    for block in user_blocks:
                        st.markdown(block.content)

            if other_blocks:
                with st.chat_message("assistant"):
                    for block in other_blocks:
                        st.markdown(block.content)

    def render_block(self, block: HistoryBlock) -> None:
        st.markdown(block.content)

    def render_streaming(self, placeholder: Any, text: str) -> None:
        if text.strip():
            placeholder.markdown(text + "▌")
