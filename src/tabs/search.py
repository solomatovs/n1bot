"""Вкладка «Поиск» — тестирование retrieval без LLM генерации."""
from __future__ import annotations

from typing import Iterator

import streamlit as st

from bootstrap import AppServices
from errors import AppError, EmptyContextError
from pipeline.events import StageCompleted, StageStarted
from query_pipeline.events import (
    ChatEvent,
    QueryVariantsGenerated,
    RetrievalDone,
    RetrievalStarted,
)
from rag import run_search_pipeline
from ui.components import (
    collection_selector,
    render_prompt_settings,
    render_search_settings,
)
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Поиск релевантного контекста")

    state.selected_collection = collection_selector(
        services.cfg, key="select_collection_search", current=state.selected_collection,
    )

    col_search, col_prompts = st.columns([1, 1])
    search_params = render_search_settings(col_search)
    prompt_params = render_prompt_settings(col_prompts)

    query = st.text_input("Запрос для поиска", key="search_query")

    if not st.button("Найти", key="btn_search"):
        return

    if not query:
        st.warning("Введите запрос.")
        return

    try:
        pipeline = run_search_pipeline(
            collection_name=str(state.selected_collection),
            query=query,
            params=search_params,
            prompts=prompt_params,
            services=services,
        )
        _consume_search_pipeline(pipeline)
    except EmptyContextError:
        st.info("Не найдено релевантных документов.")
    except AppError as e:
        st.error(f"Ошибка: {e}")


# ---------------------------------------------------------------------------
# Потребитель search pipeline
# ---------------------------------------------------------------------------

def _consume_search_pipeline(pipeline: Iterator[ChatEvent]) -> None:
    """Итерирует retrieval-пайплайн, отображая результаты поиска."""
    status_ph = st.empty()

    for event in pipeline:
        match event:
            case StageStarted(stage=name):
                status_ph.caption(f"{name}...")

            case StageCompleted(stage=name, detail=d):
                status_ph.caption(f"{name}: {d}")

            case RetrievalStarted():
                status_ph.caption("Ищу релевантный контекст…")

            case QueryVariantsGenerated(variants=vs):
                with st.expander("Переформулировки запроса", expanded=False):
                    for v in vs:
                        st.markdown(f"- {v}")

            case RetrievalDone(documents_found=n, context=ctx, sources_block=sb):
                status_ph.empty()

                st.subheader(f"Найдено документов: {n}")

                with st.expander("Контекст (как будет отправлен в LLM)", expanded=True):
                    st.code(ctx, language="markdown")

                if sb:
                    with st.expander("Источники", expanded=False):
                        st.markdown(sb)
