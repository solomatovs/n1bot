"""Простой рендерер — единственная точка отображения чата.

Форматирование повторяет структуру chat_history.md:
User → plain, Search/Context/Thinking → <details>, Assistant → plain.
"""
from __future__ import annotations

from typing import Any, List

import streamlit as st

from domain.doc_chat import BlockType, DocChatExchange, HistoryBlock, DETAIL_LABELS


class SimpleChatRenderer:
    """Рендер: chat-пузыри + markdown-форматирование, идентичное файлу."""

    def render_history(self, exchanges: List[DocChatExchange]) -> None:
        for exchange in exchanges:
            user_blocks = [b for b in exchange.blocks if b.block_type == BlockType.USER]
            other_blocks = [b for b in exchange.blocks if b.block_type != BlockType.USER]

            if user_blocks:
                with st.chat_message("user"):
                    for block in user_blocks:
                        self.render_block(block)

            if other_blocks:
                with st.chat_message("assistant"):
                    for block in other_blocks:
                        self.render_block(block)

    def render_block(self, block: HistoryBlock) -> None:
        match block.block_type:
            case BlockType.USER:
                st.markdown(block.content)
            case BlockType.SEARCH | BlockType.CONTEXT | BlockType.THINKING:
                label = DETAIL_LABELS[block.block_type]
                st.markdown(
                    f"<details>\n<summary>{label}</summary>\n\n"
                    f"{block.content}\n\n"
                    f"</details>",
                    unsafe_allow_html=True,
                )
            case BlockType.ASSISTANT:
                st.markdown(block.content)

    def render_streaming(
        self,
        placeholder: Any,
        block_type: BlockType,
        text: str,
    ) -> None:
        """Стриминг контента в placeholder с курсором ▌."""
        if not text.strip():
            return
        if block_type in DETAIL_LABELS:
            label = DETAIL_LABELS[block_type]
            placeholder.markdown(
                f"<details open>\n<summary>{label}</summary>\n\n"
                f"{text}▌\n\n"
                f"</details>",
                unsafe_allow_html=True,
            )
        else:
            placeholder.markdown(text + "▌")
