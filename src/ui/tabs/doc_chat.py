"""Вкладка «Чат по документам» — вопросы по файлам из папки.

Архитектура:
    1. Pipeline events → блоки → MarkdownBlockWriter → chat_history.md
    2. При rerun: MarkdownBlockReader читает файл → renderer отображает
    3. Файл — единственный источник правды
"""
from __future__ import annotations

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
    BlockType,
    HistoryBlock,
    MarkdownBlockReader,
    MarkdownBlockWriter,
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
    writer = MarkdownBlockWriter(history_path)
    renderer = services.create_chat_renderer()

    # Replay — всё из файла
    exchanges = _load_history(history_path)
    renderer.render_history(exchanges)

    user_prompt = st.chat_input("Введите ваш вопрос…", key="dc_chat_input")
    if not user_prompt:
        return

    # Записать вопрос в файл
    writer.write_block(HistoryBlock(BlockType.USER, user_prompt))

    with st.chat_message("user"):
        st.markdown(user_prompt)

    with st.chat_message("assistant"):
        pipeline = create_doc_pipeline(services)
        ctx = create_doc_context(
            folder_path=folder_path,
            query=user_prompt,
            model=active_model,
            services=services,
        )
        _consume_pipeline(pipeline.run(ctx), writer, renderer)

    writer.write_separator()
    st.rerun()


# ---------------------------------------------------------------------------
# Pipeline consumer — пишет в файл, стримит через renderer
# ---------------------------------------------------------------------------

def _consume_pipeline(
    events: Iterator[DocPipelineEvent],
    writer: MarkdownBlockWriter,
    renderer,
) -> None:
    status_ph = st.empty()

    thinking_tokens: list[str] = []
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

            # --- Поиск → сразу в файл ---
            case SearchDone(hits=hits):
                if hits:
                    block = HistoryBlock(BlockType.SEARCH, _format_search(hits))
                    writer.write_block(block)
                    renderer.render_block(block)

            # --- Контекст → сразу в файл ---
            case ContextReady(fragments=frags):
                if frags:
                    block = HistoryBlock(BlockType.CONTEXT, _format_context(frags))
                    writer.write_block(block)
                    renderer.render_block(block)

            # --- Стриминг размышлений ---
            case ThinkingToken(token=tok):
                if thinking_ph is None:
                    thinking_ph = st.empty()
                thinking_tokens.append(tok)
                renderer.render_streaming(thinking_ph, "".join(thinking_tokens))

            # --- Стриминг ответа ---
            case AnswerToken(token=tok):
                if thinking_tokens and answer_ph is None:
                    _finalize_streaming(thinking_ph, thinking_tokens, BlockType.THINKING, writer)
                    thinking_tokens.clear()
                    thinking_ph = None

                if answer_ph is None:
                    answer_ph = st.empty()
                answer_tokens.append(tok)
                renderer.render_streaming(answer_ph, "".join(answer_tokens))

            case GenerationDone():
                status_ph.empty()

    # Финализация: записать в файл
    if thinking_tokens:
        _finalize_streaming(thinking_ph, thinking_tokens, BlockType.THINKING, writer)
    if answer_tokens:
        _finalize_streaming(answer_ph, answer_tokens, BlockType.ASSISTANT, writer)


def _finalize_streaming(
    placeholder: Any,
    tokens: list[str],
    block_type: BlockType,
    writer: MarkdownBlockWriter,
) -> None:
    text = "".join(tokens)
    if not text.strip():
        return
    block = HistoryBlock(block_type, text)
    writer.write_block(block)
    if placeholder is not None:
        placeholder.markdown(text)


# ---------------------------------------------------------------------------
# Загрузка истории
# ---------------------------------------------------------------------------

def _load_history(path: Path):
    if not path.exists():
        return []
    return MarkdownBlockReader().read_exchanges(path.read_text(encoding="utf-8"))


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
            f"```\n{hit.content[:300]}\n```"
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
            f"```\n{frag.text[:500]}\n```"
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
