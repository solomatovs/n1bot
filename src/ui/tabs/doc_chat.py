"""Вкладка «Чат по документам».

Layout:
    Sidebar: выбор папки, список чатов, кнопка «Новый чат», выбор модели
    Main:    история чата + chat_input (pinned to bottom)

Архитектура (CQRS через файл):
    Write: pipeline events → writer → {chat_id}.jsonl
    Read:  {chat_id}.jsonl → reader → render_event
    Файл — единственный источник правды.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def render(services: AppServices, state: SessionState) -> None:
    cfg = services.cfg

    # --- Sidebar: папка, чаты, модель ---
    folder_path = _sidebar_folder(cfg)
    if folder_path is None:
        st.info("Выберите папку с документами в боковой панели.")
        return

    boba_path = cfg.boba_path(folder_path)
    boba_path.mkdir(exist_ok=True)

    chats_dir = cfg.chats_dir(folder_path)
    chats_dir.mkdir(exist_ok=True)

    chat_id = _sidebar_chats(chats_dir)
    active_model = _sidebar_model(cfg)

    # --- Main: чат ---
    history_path = cfg.chat_history_path(folder_path, chat_id)
    writer = JsonlChatWriter(history_path)

    with JsonlChatReader(history_path) as reader:
        # Replay всей истории
        for event in reader.read():
            _render_event(event)

        # Chat input (pinned to bottom)
        user_prompt = st.chat_input("Введите ваш вопрос…")
        if not user_prompt:
            return

        # Write → file → read → render
        exchange_id = writer.new_exchange()
        writer.write(exchange_id, EventType.USER, user_prompt)
        for event in reader.read():
            _render_event(event)

        # Pipeline
        pipeline = create_doc_pipeline(services)
        ctx = create_doc_context(
            folder_path=folder_path,
            query=user_prompt,
            model=active_model,
            services=services,
            history_path=history_path,
        )
        _consume_pipeline(pipeline.run(ctx), writer, reader, exchange_id)

    st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _sidebar_folder(cfg: AppConfig) -> Path | None:
    """Выбор папки с документами."""
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name for d in base_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not folders:
        with st.sidebar:
            st.warning("Нет папок. Импортируйте документы.")
        return None

    with st.sidebar:
        selected = st.selectbox("Папка с документами", folders, key="dc_folder")

    if not selected:
        return None
    return base_dir / selected


def _sidebar_chats(chats_dir: Path) -> str:
    """Список чатов + кнопка «Новый чат». Возвращает chat_id."""
    chat_files = sorted(chats_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    chat_ids = [f.stem for f in chat_files]

    with st.sidebar:
        st.divider()

        if st.button("Новый чат", use_container_width=True, key="dc_new_chat"):
            new_id = _new_chat_id()
            st.session_state["dc_chat_id"] = new_id
            st.rerun()

        if not chat_ids:
            chat_id = _new_chat_id()
            st.session_state["dc_chat_id"] = chat_id
            return chat_id

        # Текущий выбранный чат
        current = st.session_state.get("dc_chat_id", chat_ids[0])
        if current not in chat_ids:
            chat_ids.insert(0, current)

        selected_idx = chat_ids.index(current) if current in chat_ids else 0
        selected = st.selectbox(
            "Чат",
            chat_ids,
            index=selected_idx,
            format_func=lambda cid: _chat_label(chats_dir, cid),
            key="dc_chat_select",
        )
        st.session_state["dc_chat_id"] = selected
        return selected


def _sidebar_model(cfg: AppConfig) -> str:
    """Выбор модели."""
    with st.sidebar:
        st.divider()
        return model_selector(cfg, key="dc_model")


def _new_chat_id() -> str:
    """Сгенерировать ID нового чата (timestamp + short uuid)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"{ts}_{short}"


def _chat_label(chats_dir: Path, chat_id: str) -> str:
    """Человекочитаемая метка чата для selectbox."""
    path = chats_dir / f"{chat_id}.jsonl"
    if not path.exists() or path.stat().st_size == 0:
        return f"{chat_id} (новый)"

    # Берём первое user-сообщение как превью
    with JsonlChatReader(path) as reader:
        for event in reader.read():
            if event.is_user:
                preview = event.content[:50]
                if len(event.content) > 50:
                    preview += "…"
                return preview
    return chat_id


# ---------------------------------------------------------------------------
# Рендеринг событий
# ---------------------------------------------------------------------------

def _render_event(event) -> None:
    """Отрисовать одно событие чата."""
    role = "user" if event.is_user else "assistant"
    with st.chat_message(role):
        if event.event_type.collapsible:
            with st.expander(event.event_type.label):
                st.markdown(event.content)
        else:
            st.markdown(event.content)


# ---------------------------------------------------------------------------
# Pipeline consumer
# ---------------------------------------------------------------------------

def _consume_pipeline(
    events: Iterator[DocPipelineEvent],
    writer: JsonlChatWriter,
    reader: JsonlChatReader,
    exchange_id: str,
) -> None:
    status_ph = st.empty()

    thinking_tokens: list[str] = []
    thinking_expander: Any = None
    thinking_ph: Any = None
    answer_tokens: list[str] = []
    answer_ph: Any = None

    for event in events:
        match event:
            # --- Статусные ---
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

            # --- Поиск: write → file → read → render ---
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
                        _render_event(chat_event)

            # --- Контекст: write → file → read → render ---
            case ContextReady(fragments=frags):
                if frags:
                    meta = ContextMeta(fragment_count=len(frags))
                    writer.write(
                        exchange_id, EventType.CONTEXT,
                        _format_context(frags),
                        metadata=asdict(meta),
                    )
                    for chat_event in reader.read():
                        _render_event(chat_event)

            # --- Стриминг размышлений (эфемерный превью) ---
            case ThinkingToken(token=tok):
                if thinking_expander is None:
                    thinking_expander = st.expander(EventType.THINKING.label, expanded=True)
                    thinking_ph = thinking_expander.empty()
                thinking_tokens.append(tok)
                if thinking_ph is not None and "".join(thinking_tokens).strip():
                    thinking_ph.markdown("".join(thinking_tokens) + "▌")

            # --- Стриминг ответа (эфемерный превью) ---
            case AnswerToken(token=tok):
                if thinking_tokens and answer_ph is None:
                    text = "".join(thinking_tokens)
                    if text.strip():
                        writer.write(exchange_id, EventType.THINKING, text)
                    thinking_tokens.clear()
                    thinking_ph = None
                    thinking_expander = None

                if answer_ph is None:
                    answer_ph = st.empty()
                answer_tokens.append(tok)
                text = "".join(answer_tokens)
                if text.strip():
                    answer_ph.markdown(text + "▌")

            case GenerationDone():
                status_ph.empty()

    # Финализация: запись в файл (рендер после st.rerun)
    if thinking_tokens:
        text = "".join(thinking_tokens)
        if text.strip():
            writer.write(exchange_id, EventType.THINKING, text)
    if answer_tokens:
        writer.write(exchange_id, EventType.ASSISTANT, "".join(answer_tokens))


# ---------------------------------------------------------------------------
# Форматирование
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
