"""Вкладка «Чат по документам» — вопросы по файлам из папки.

Архитектура (CQRS через файл):
    Write side: pipeline events → writer → chat_history.jsonl
    Read side:  chat_history.jsonl → reader/tail → renderer

    UI никогда не получает данные от writer напрямую.
    Файл — единственное узкое горлышко между записью и отображением.

    Стриминг (thinking/answer) — эфемерный превью через render_streaming.
    После st.rerun() всё перерисовывается из файла.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

import streamlit as st

from application.doc_pipeline.events import (
    AnswerToken,
    ContextReady,
    DocPipelineEvent,
    FileIndexed,
    GenerationDone,
    IndexingDone,
    IndexingSkipped,
    SearchDone,
    ThinkingToken,
)
from application.doc_pipeline.factory import create_doc_context, create_doc_pipeline
from domain.config import AppConfig
from domain.doc_chat import (
    ContextMeta,
    EventType,
    JsonlChatReader,
    JsonlChatWriter,
    SearchHitMeta,
    SearchMeta,
)
from domain.doc_search import Fragment, SearchHit
from domain.pipeline import StageCompleted, StageStarted
from infrastructure.bootstrap import AppServices
from ui.components.selectors import model_selector
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Чат по документам")

    cfg = services.cfg
    folder_path = _folder_selector_readonly(cfg)
    if folder_path is None:
        return

    _reset_on_folder_change(folder_path.name)

    active_model = model_selector(cfg, key="dc_model")

    boba_path = cfg.boba_path(folder_path)
    boba_path.mkdir(exist_ok=True)

    history_path = cfg.chat_history_path(folder_path)
    writer = JsonlChatWriter(history_path)
    renderer = services.create_chat_renderer()

    with JsonlChatReader(history_path) as reader:
        # Replay: рендерим всю историю из файла (reader запоминает позицию)
        for event in reader.read():
            renderer.render_event(event)

        user_prompt = st.chat_input("Введите ваш вопрос…", key="dc_chat_input")
        if not user_prompt:
            return

        # Write side: записать вопрос в файл
        exchange_id = writer.new_exchange()
        writer.write(exchange_id, EventType.USER, user_prompt)

        # Read side: прочитать новое из файла → отрендерить
        for event in reader.read():
            renderer.render_event(event)

        # Pipeline
        pipeline = create_doc_pipeline(services)
        ctx = create_doc_context(
            folder_path=folder_path,
            query=user_prompt,
            model=active_model,
            services=services,
            history_path=history_path,
        )
        _consume_pipeline(pipeline.run(ctx), writer, reader, exchange_id, renderer)

    # Rerun → всё перерисуется из файла
    st.rerun()


# ---------------------------------------------------------------------------
# Pipeline consumer
# ---------------------------------------------------------------------------

def _consume_pipeline(
    events: Iterator[DocPipelineEvent],
    writer: JsonlChatWriter,
    reader: JsonlChatReader,
    exchange_id: str,
    renderer,
) -> None:
    status_ph = st.empty()

    thinking_tokens: list[str] = []
    thinking_expander: Any = None
    thinking_ph: Any = None
    answer_tokens: list[str] = []
    answer_ph: Any = None

    for event in events:
        match event:
            # --- Статусные (не сохраняются в историю) ---
            case StageStarted(stage=name):
                status_ph.caption(f"⏳ {name}...")

            case StageCompleted(stage=name, detail=d):
                status_ph.caption(f"✓ {name}: {d}")

            case IndexingSkipped(collection=_, doc_count=n):
                status_ph.caption(f"Индекс найден ({n} чанков)")

            case FileIndexed(filename=name, chunks=c, index=i, total=total):
                status_ph.caption(f"Индексация: {i}/{total} — {name} ({c} чанков)")

            case IndexingDone(total_files=f, total_chunks=c):
                status_ph.caption(f"Индексация: {f} файлов, {c} чанков")

            # --- Поиск: write → file → tail → render ---
            case SearchDone(hits=hits):
                if hits:
                    meta = SearchMeta(hits=[
                        SearchHitMeta(
                            source_file=h.location.source_file,
                            start_line=h.location.start_line,
                            end_line=h.location.end_line,
                            score=h.score,
                        )
                        for h in hits
                    ])
                    writer.write(
                        exchange_id, EventType.SEARCH,
                        _format_search(hits),
                        metadata=asdict(meta),
                    )
                    for chat_event in reader.read():
                        renderer.render_event(chat_event)

            # --- Контекст: write → file → tail → render ---
            case ContextReady(fragments=frags):
                if frags:
                    meta = ContextMeta(fragment_count=len(frags))
                    writer.write(
                        exchange_id, EventType.CONTEXT,
                        _format_context(frags),
                        metadata=asdict(meta),
                    )
                    for chat_event in reader.read():
                        renderer.render_event(chat_event)

            # --- Стриминг размышлений (эфемерный превью) ---
            case ThinkingToken(token=tok):
                if thinking_expander is None:
                    thinking_expander = st.expander(EventType.THINKING.label, expanded=True)
                    thinking_ph = thinking_expander.empty()
                thinking_tokens.append(tok)
                renderer.render_streaming(thinking_ph, "".join(thinking_tokens))

            # --- Стриминг ответа (эфемерный превью) ---
            case AnswerToken(token=tok):
                if thinking_tokens and answer_ph is None:
                    # Финализация thinking: записать в файл
                    text = "".join(thinking_tokens)
                    if text.strip():
                        writer.write(exchange_id, EventType.THINKING, text)
                    thinking_tokens.clear()
                    thinking_ph = None
                    thinking_expander = None

                if answer_ph is None:
                    answer_ph = st.empty()
                answer_tokens.append(tok)
                renderer.render_streaming(answer_ph, "".join(answer_tokens))

            case GenerationDone():
                status_ph.empty()

    # Финализация: только запись в файл (рендер после st.rerun)
    if thinking_tokens:
        text = "".join(thinking_tokens)
        if text.strip():
            writer.write(exchange_id, EventType.THINKING, text)
    if answer_tokens:
        writer.write(exchange_id, EventType.ASSISTANT, "".join(answer_tokens))


# ---------------------------------------------------------------------------
# Форматирование блоков
# ---------------------------------------------------------------------------

def _format_search(hits: list[SearchHit]) -> str:
    parts = []
    for hit in hits:
        loc = hit.location
        parts.append(
            f"**{loc.source_file}:{loc.start_line}-{loc.end_line}** "
            f"(секция: {loc.section_title}, score: {hit.score:.2f})\n\n"
            f"```\n{hit.content}\n```"
        )
    return "\n\n".join(parts)


def _format_context(fragments: list[Fragment]) -> str:
    parts = []
    for frag in fragments:
        loc = frag.hit.location
        parts.append(
            f"**{loc.source_file}:{frag.read_start_line}-{frag.read_end_line}** "
            f"(чанк: {loc.start_line}-{loc.end_line}, "
            f"секция: {loc.section_title}, score: {frag.hit.score:.2f})\n\n"
            f"```\n{frag.text}\n```"
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Выбор папки
# ---------------------------------------------------------------------------

def _folder_selector_readonly(cfg: AppConfig) -> Path | None:
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name for d in base_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not folders:
        st.warning("Нет доступных папок. Импортируйте документы во вкладке «Загрузка вручную».")
        return None

    selected = st.selectbox("Папка с документами", folders, key="dc_folder")
    if not selected:
        return None

    return base_dir / selected


def _reset_on_folder_change(current_folder: str) -> None:
    prev = st.session_state.get("dc_prev_folder")
    if prev != current_folder:
        st.session_state["dc_prev_folder"] = current_folder
