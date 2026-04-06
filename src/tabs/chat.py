"""Вкладка «Чат» — ответы на вопросы по базе знаний."""
from __future__ import annotations

from typing import Iterator

import streamlit as st

from errors import AppError, EmptyContextError
from events import (
    AnswerToken,
    ChatEvent,
    GenerationDone,
    RetrievalDone,
    RetrievalStarted,
    ThinkingToken,
)
from pipeline.events import QueryVariantsGenerated, StageCompleted, StageStarted
from rag import run_chat_pipeline
from ui.components import (
    collection_selector,
    extract_page_ids_from_answer,
    model_selector,
    render_chat_history,
    render_prompt_settings,
    render_search_settings,
)
from ui.state import AppConfig, ChatMessage, SearchParams, SessionState


def render(cfg: AppConfig, state: SessionState) -> None:
    st.title("N1 Hub AI bots")

    state.selected_collection = collection_selector(
        cfg, key="select_collection_chat", current=state.selected_collection,
    )

    col_model, col_search, col_prompts = st.columns([3, 1, 1])
    with col_model:
        active_model = model_selector(cfg)
    search_params = render_search_settings(col_search)
    prompt_params = render_prompt_settings(col_prompts)

    render_chat_history(state.chat_history)

    user_prompt = st.chat_input("Введите ваш вопрос…")
    if not user_prompt:
        _render_status_bar(state, search_params)
        return

    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        try:
            pipeline = run_chat_pipeline(
                collection_name=str(state.selected_collection),
                query=user_prompt,
                model=active_model,
                params=search_params,
                prompts=prompt_params,
                cfg=cfg,
            )
            result = _consume_chat_pipeline(pipeline)
        except EmptyContextError:
            result = _ChatResult(answer="Я не нашёл релевантный контекст по вашей коллекции.")
            st.markdown(result.answer)
        except AppError as e:
            result = _ChatResult(answer=f"Ошибка: {e}")
            st.error(result.answer)

    state.push_message(ChatMessage(
        question=user_prompt,
        answer=result.answer,
        thinking=result.thinking,
        rag_context=result.rag_context,
    ))
    state.used_page_ids[user_prompt] = extract_page_ids_from_answer(result.answer)

    _render_status_bar(state, search_params)


# ---------------------------------------------------------------------------
# Результат чата (заполняется в _consume_chat_pipeline)
# ---------------------------------------------------------------------------

class _ChatResult:
    __slots__ = ("answer", "thinking", "rag_context", "sources_block")

    def __init__(
        self,
        answer: str = "",
        thinking: str = "",
        rag_context: str = "",
        sources_block: str = "",
    ) -> None:
        self.answer = answer
        self.thinking = thinking
        self.rag_context = rag_context
        self.sources_block = sources_block


# ---------------------------------------------------------------------------
# Потребитель chat pipeline — единственное место с Streamlit-виджетами
# ---------------------------------------------------------------------------

def _consume_chat_pipeline(pipeline: Iterator[ChatEvent]) -> _ChatResult:
    """Итерирует генератор чат-пайплайна, обновляя Streamlit-виджеты по событиям."""
    result = _ChatResult()

    thinking_expander = st.expander("Процесс размышления", expanded=True)
    thinking_ph = thinking_expander.empty()
    answer_ph = st.empty()

    status_ph = st.empty()

    for event in pipeline:
        match event:
            case StageStarted(stage=name):
                status_ph.caption(f"⏳ {name}...")

            case StageCompleted(stage=name, detail=d):
                status_ph.caption(f"✓ {name}: {d}")

            case QueryVariantsGenerated(variants=vs):
                with st.expander("Переформулировки запроса", expanded=False):
                    for v in vs:
                        st.markdown(f"- {v}")

            case RetrievalStarted():
                status_ph.caption("Ищу релевантный контекст…")

            case RetrievalDone(context=ctx, sources_block=sb):
                result.rag_context = ctx
                result.sources_block = sb
                with st.expander("Найденный контекст из базы знаний", expanded=False):
                    st.markdown(ctx)

            case ThinkingToken(token=tok):
                result.thinking += tok
                thinking_ph.markdown(result.thinking + "▌")

            case AnswerToken(token=tok):
                result.answer += tok
                if result.answer.strip():
                    answer_ph.markdown(result.answer + "▌")

            case GenerationDone():
                status_ph.empty()

    # Финализация
    if result.thinking.strip():
        thinking_ph.markdown(result.thinking)
    else:
        thinking_expander.empty()

    if result.sources_block:
        result.answer = f"{result.answer}\n\n---\n**Источники:**\n{result.sources_block}"

    if result.answer.strip():
        answer_ph.markdown(result.answer)

    return result


# ---------------------------------------------------------------------------
# Статусная строка
# ---------------------------------------------------------------------------

def _render_status_bar(state: SessionState, params: SearchParams) -> None:
    st.caption(
        f"Модель: **{state.selected_model_name}** · "
        f"Коллекция: **{state.selected_collection}** · "
        f"Multi-query: {'ON' if params.use_multi_query else 'OFF'} · "
        f"Документов: {params.answers_per_variant} · "
        f"t={params.temperature}"
    )
