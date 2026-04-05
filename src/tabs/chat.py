"""Вкладка «Чат» — ответы на вопросы по базе знаний."""
from __future__ import annotations

import streamlit as st

from rag import prepare_rag_context
from ui.components import (
    collection_selector,
    extract_page_ids_from_answer,
    model_selector,
    render_chat_history,
)
from ui.state import AppConfig, ChatMessage, SessionState
from ui.streaming import StreamRenderer


def render(cfg: AppConfig, state: SessionState) -> None:
    st.title("N1 Hub AI bots")

    state.selected_collection = collection_selector(
        cfg, key="select_collection_chat", current=state.selected_collection,
    )

    cols = st.columns([3, 1, 1])
    with cols[0]:
        active_model = model_selector(cfg)
    with cols[1]:
        use_mq = st.checkbox("Multi-query", value=True, help="Переформулировки + RRF")

    render_chat_history(state.chat_history)

    user_prompt = st.chat_input("Введите ваш вопрос…")
    if not user_prompt:
        _render_status_bar(cfg, state, use_mq)
        return

    thinking_text = ""
    rag_context = ""
    reply = ""
    oai_client = None

    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        oai_client, messages, sources_block = _fetch_context(cfg, state, user_prompt, active_model, use_mq)

        if messages:
            rag_context = _extract_rag_context(messages)

        if oai_client is None or messages is None:
            reply = "Я не нашёл релевантный контекст по вашей коллекции."
            st.markdown(reply)
        else:
            if rag_context:
                with st.expander("Найденный контекст из базы знаний", expanded=False):
                    st.markdown(rag_context)

            renderer = StreamRenderer(st.container())
            stream = oai_client.chat.completions.create(
                model=active_model,
                messages=messages,
                temperature=0,
                stream=True,
            )
            for chunk in stream:
                renderer.feed(chunk.choices[0].delta)
            renderer.finalise()

            if sources_block:
                renderer.set_answer(
                    f"{renderer.state.answer}\n\n---\n**Источники:**\n{sources_block}"
                )

            reply = renderer.state.answer
            thinking_text = renderer.state.thinking

    state.push_message(ChatMessage(
        question=user_prompt,
        answer=reply,
        thinking=thinking_text,
        rag_context=rag_context,
    ))
    state.used_page_ids[user_prompt] = extract_page_ids_from_answer(reply)

    _render_status_bar(cfg, state, use_mq)


# ---------------------------------------------------------------------------
# Приватные вспомогательные функции
# ---------------------------------------------------------------------------

def _fetch_context(cfg, state, prompt, model, use_mq):
    with st.spinner("Ищу контекст…"):
        try:
            return prepare_rag_context(
                embed_collection_name=str(state.selected_collection),
                query=prompt,
                model=model,
                top_n=12,
                db_path=cfg.chroma_db_path,
                llm_base_url=cfg.litellm_url,
                embedding_model=None,
                use_multi_query=bool(use_mq),
                mq_variants=3,
                k_per_variant=6,
                variant_offset=0,
                exclude_page_ids=[],
                answers_per_variant=3,
            )
        except Exception as e:
            st.error(f"Ошибка: {e}")
            return None, None, None


def _extract_rag_context(messages) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def _render_status_bar(cfg, state, use_mq):
    st.caption(
        f"Текущая модель: **{state.selected_model_name}** · "
        f"Коллекция: **{state.selected_collection}** · "
        f"Multi-query: {'ON' if use_mq else 'OFF'}"
    )
