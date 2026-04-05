"""Вкладка «Чат» — ответы на вопросы по базе знаний."""
from __future__ import annotations

from typing import Optional

import streamlit as st

from rag import RagContext, prepare_rag_context
from ui.components import (
    collection_selector,
    extract_page_ids_from_answer,
    model_selector,
    render_chat_history,
    render_search_settings,
)
from ui.state import AppConfig, ChatMessage, SearchParams, SessionState
from ui.streaming import StreamRenderer


def render(cfg: AppConfig, state: SessionState) -> None:
    st.title("N1 Hub AI bots")

    state.selected_collection = collection_selector(
        cfg, key="select_collection_chat", current=state.selected_collection,
    )

    col_model, col_settings = st.columns([3, 1])
    with col_model:
        active_model = model_selector(cfg)
    search_params = render_search_settings(col_settings)

    render_chat_history(state.chat_history)

    user_prompt = st.chat_input("Введите ваш вопрос…")
    if not user_prompt:
        _render_status_bar(state, search_params)
        return

    thinking_text = ""
    rag_context = ""
    reply = ""

    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        ctx = _fetch_context(cfg, state, user_prompt, active_model, search_params)

        if ctx is not None:
            rag_context = _extract_rag_context(ctx.messages)

        if ctx is None:
            reply = "Я не нашёл релевантный контекст по вашей коллекции."
            st.markdown(reply)
        else:
            if rag_context:
                with st.expander("Найденный контекст из базы знаний", expanded=False):
                    st.markdown(rag_context)

            renderer = StreamRenderer(st.container())
            stream = ctx.client.chat.completions.create(
                model=active_model,
                messages=ctx.messages,
                temperature=search_params.temperature,
                stream=True,
            )
            for chunk in stream:
                renderer.feed(chunk.choices[0].delta)
            renderer.finalise()

            if ctx.sources_block:
                renderer.set_answer(
                    f"{renderer.state.answer}\n\n---\n**Источники:**\n{ctx.sources_block}"
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

    _render_status_bar(state, search_params)


# ---------------------------------------------------------------------------
# Приватные вспомогательные функции
# ---------------------------------------------------------------------------

def _fetch_context(
    cfg: AppConfig,
    state: SessionState,
    prompt: str,
    model: str,
    params: SearchParams,
) -> Optional[RagContext]:
    with st.spinner("Ищу контекст…"):
        try:
            return prepare_rag_context(
                embed_collection_name=str(state.selected_collection),
                query=prompt,
                model=model,
                params=params,
                db_path=cfg.chroma_db_path,
                llm_base_url=cfg.litellm_url,
            )
        except Exception as e:
            st.error(f"Ошибка: {e}")
            return None


def _extract_rag_context(messages: list) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


def _render_status_bar(state: SessionState, params: SearchParams) -> None:
    st.caption(
        f"Модель: **{state.selected_model_name}** · "
        f"Коллекция: **{state.selected_collection}** · "
        f"Multi-query: {'ON' if params.use_multi_query else 'OFF'} · "
        f"Документов: {params.answers_per_variant} · "
        f"t={params.temperature}"
    )
