"""Богатый рендерер — chat-пузыри, expander-ы, кликабельные ссылки на файлы."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import streamlit as st

from domain.doc_chat import BlockType, DocChatExchange, HistoryBlock

_FILE_LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")


class RichChatRenderer:
    """Полнофункциональный рендер: chat-пузыри, expander-ы, ссылки [[file]]."""

    def __init__(self, context_path: Path | None = None) -> None:
        self._context_path = context_path

    def render_history(self, exchanges: List[DocChatExchange]) -> None:
        for exchange in exchanges:
            user_blocks = [b for b in exchange.blocks if b.block_type == BlockType.USER]
            assistant_blocks = [b for b in exchange.blocks if b.block_type != BlockType.USER]

            if user_blocks:
                with st.chat_message("user"):
                    for block in user_blocks:
                        self.render_block(block)

            if assistant_blocks:
                with st.chat_message("assistant"):
                    for block in assistant_blocks:
                        self.render_block(block)

        if "dc_view_file" in st.session_state:
            _show_file_dialog()

    def render_block(self, block: HistoryBlock) -> None:
        match block.block_type:
            case BlockType.USER:
                st.markdown(block.content)
            case BlockType.SEARCH:
                with st.expander("Найденные фрагменты", expanded=False):
                    self._render_with_links(block.content)
            case BlockType.CONTEXT:
                with st.expander("Контекст из документов", expanded=False):
                    self._render_with_links(block.content)
            case BlockType.THINKING:
                with st.expander("Процесс размышления", expanded=False):
                    st.markdown(block.content)
            case BlockType.ASSISTANT:
                st.markdown(block.content)

    def _render_with_links(self, content: str) -> None:
        """Рендерить контент, заменяя [[filename]] на кликабельные кнопки."""
        parts = _FILE_LINK_PATTERN.split(content)

        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part.strip():
                    st.markdown(part)
            else:
                filename = part
                btn_key = f"dc_link_{hash(content)}_{i}"
                if st.button(
                    f"📄 {filename}",
                    key=btn_key,
                    type="tertiary",
                ):
                    if self._context_path is not None:
                        st.session_state["dc_view_file"] = str(self._context_path / filename)
                        st.session_state["dc_view_filename"] = filename
                        st.rerun()


@st.dialog("Просмотр файла", width="large")
def _show_file_dialog() -> None:
    file_path_str = st.session_state.get("dc_view_file", "")
    filename = st.session_state.get("dc_view_filename", "")

    file_path = Path(file_path_str)
    if not file_path.exists():
        st.error(f"Файл не найден: {filename}")
        return

    st.subheader(filename)
    size_kb = file_path.stat().st_size / 1024
    st.caption(f"Размер: {size_kb:.1f} КБ")

    content = file_path.read_text(encoding="utf-8", errors="replace")
    st.code(content, language="markdown", line_numbers=True)

    if st.button("Закрыть", key="dc_close_viewer"):
        st.session_state.pop("dc_view_file", None)
        st.session_state.pop("dc_view_filename", None)
        st.rerun()
