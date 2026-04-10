"""Вкладка «Чат по документам» — вопросы по файлам из папки.

Архитектура истории:
    1. Pipeline events → HistoryBlock → append в chat_history.md (сразу)
    2. UI рендерит блоки из файла (replay) или из потока (live streaming)
    3. Файл — единственный источник правды
    4. Добавление нового этапа = новый BlockType + case в _consume_pipeline
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List

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
    DocChatExchange,
    HistoryBlock,
    parse_exchanges,
    serialize_block,
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

    context_path = cfg.context_path(folder_path)
    history_path = cfg.chat_history_path(folder_path)

    exchanges = _load_history(history_path)
    _render_history(exchanges, context_path)

    user_prompt = st.chat_input("Введите ваш вопрос…", key="dc_chat_input")
    if not user_prompt:
        return

    # Сразу записать вопрос в файл
    _write_block(history_path, HistoryBlock(BlockType.USER, user_prompt))

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
        _consume_pipeline(pipeline.run(ctx), history_path, context_path)

    # Записать разделитель обмена
    _write_separator(history_path)
    st.rerun()


# ---------------------------------------------------------------------------
# Pipeline consumer → блоки пишутся в файл по мере создания
# ---------------------------------------------------------------------------

def _consume_pipeline(
    events: Iterator[DocPipelineEvent],
    history_path: Path,
    context_path: Path,
) -> None:
    """Обработать события pipeline: отрисовать UI, записать блоки в файл."""
    status_ph = st.empty()

    # Аккумуляторы стриминговых токенов
    thinking_tokens: list[str] = []
    thinking_ph = None
    answer_tokens: list[str] = []
    answer_ph = None

    for event in events:
        match event:
            # --- Статусные события (не сохраняются) ---
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

            # --- Поиск → блок (сразу в файл) ---
            case SearchDone(hits=hits):
                if hits:
                    block = HistoryBlock(BlockType.SEARCH, _format_search(hits))
                    _write_block(history_path, block)
                    _render_block(block, context_path)

            # --- Контекст → блок (сразу в файл) ---
            case ContextReady(fragments=frags):
                if frags:
                    block = HistoryBlock(BlockType.CONTEXT, _format_context(frags))
                    _write_block(history_path, block)
                    _render_block(block, context_path)

            # --- Стриминг размышлений ---
            case ThinkingToken(token=tok):
                if thinking_ph is None:
                    thinking_exp = st.expander("Процесс размышления", expanded=True)
                    thinking_ph = thinking_exp.empty()
                thinking_tokens.append(tok)
                thinking_ph.markdown("".join(thinking_tokens) + "▌")

            # --- Стриминг ответа ---
            case AnswerToken(token=tok):
                if answer_ph is None:
                    answer_ph = st.empty()
                answer_tokens.append(tok)
                text = "".join(answer_tokens)
                if text.strip():
                    answer_ph.markdown(text + "▌")

            case GenerationDone():
                status_ph.empty()

    # Финализация стриминговых блоков → в файл
    if thinking_tokens:
        thinking_text = "".join(thinking_tokens)
        if thinking_ph is not None and thinking_text.strip():
            thinking_ph.markdown(thinking_text)
        _write_block(history_path, HistoryBlock(BlockType.THINKING, thinking_text))

    if answer_tokens:
        answer_text = "".join(answer_tokens)
        if answer_ph is not None and answer_text.strip():
            answer_ph.markdown(answer_text)
        _write_block(history_path, HistoryBlock(BlockType.ASSISTANT, answer_text))


# ---------------------------------------------------------------------------
# Форматирование блоков — полный контент, идентичный отображению
# ---------------------------------------------------------------------------

def _format_search(hits: list[SearchHit]) -> str:
    """Форматировать результаты поиска. Ссылки [[file.md]] — кликабельны в UI."""
    parts = []
    for hit in hits:
        loc = hit.location
        parts.append(
            f"[[{loc.source_file}]]:{loc.start_line}-{loc.end_line} "
            f"(секция: {loc.section_title}, score: {hit.score:.2f})\n\n"
            f"```\n{hit.content[:300]}\n```"
        )
    return "\n\n".join(parts)


def _format_context(fragments: list[Fragment]) -> str:
    """Форматировать контекст. Ссылки [[file.md]] — кликабельны в UI."""
    parts = []
    for frag in fragments:
        loc = frag.hit.location
        parts.append(
            f"[[{loc.source_file}]]:{frag.read_start_line}-{frag.read_end_line} "
            f"(чанк: {loc.start_line}-{loc.end_line}, "
            f"секция: {loc.section_title}, score: {frag.hit.score:.2f})\n\n"
            f"```\n{frag.text[:500]}\n```"
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Рендер блоков — единый для live и replay
# ---------------------------------------------------------------------------

_FILE_LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")


def _render_block(block: HistoryBlock, context_path: Path | None = None) -> None:
    """Отрисовать один блок. Одинаково для live-потока и воспроизведения истории.

    Ссылки [[filename.md]] в тексте рендерятся как кнопки открытия файла.
    """
    match block.block_type:
        case BlockType.USER:
            st.markdown(block.content)
        case BlockType.SEARCH:
            with st.expander("Найденные фрагменты", expanded=False):
                _render_content_with_links(block.content, context_path)
        case BlockType.CONTEXT:
            with st.expander("Контекст из документов", expanded=False):
                _render_content_with_links(block.content, context_path)
        case BlockType.THINKING:
            with st.expander("Процесс размышления", expanded=False):
                st.markdown(block.content)
        case BlockType.ASSISTANT:
            st.markdown(block.content)


def _render_content_with_links(content: str, context_path: Path | None) -> None:
    """Рендерить контент, заменяя [[filename]] на кликабельные кнопки."""
    parts = _FILE_LINK_PATTERN.split(content)

    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Обычный текст
            if part.strip():
                st.markdown(part)
        else:
            # Имя файла из [[...]]
            filename = part
            btn_key = f"dc_link_{hash(content)}_{i}"
            if st.button(
                f"📄 {filename}",
                key=btn_key,
                type="tertiary",
            ):
                if context_path is not None:
                    st.session_state["dc_view_file"] = str(context_path / filename)
                    st.session_state["dc_view_filename"] = filename
                    st.rerun()


# ---------------------------------------------------------------------------
# Файл диалог
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# История — файл как единственный источник правды
# ---------------------------------------------------------------------------

def _load_history(path: Path) -> List[DocChatExchange]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return parse_exchanges(text)


def _write_block(path: Path, block: HistoryBlock) -> None:
    """Дописать один блок в файл (append)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(serialize_block(block))


def _write_separator(path: Path) -> None:
    """Дописать разделитель обмена."""
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n---\n\n")


def _render_history(exchanges: List[DocChatExchange], context_path: Path) -> None:
    """Воспроизвести историю — каждый обмен: user + assistant."""
    for exchange in exchanges:
        user_blocks = [b for b in exchange.blocks if b.block_type == BlockType.USER]
        assistant_blocks = [b for b in exchange.blocks if b.block_type != BlockType.USER]

        if user_blocks:
            with st.chat_message("user"):
                for block in user_blocks:
                    _render_block(block, context_path)

        if assistant_blocks:
            with st.chat_message("assistant"):
                for block in assistant_blocks:
                    _render_block(block, context_path)

    if "dc_view_file" in st.session_state:
        _show_file_dialog()


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
        st.warning("Нет доступных папок. Импортируйте документы во вкладке «Документы».")
        return None

    selected = st.selectbox("Папка с документами", folders, key="dc_folder")
    if not selected:
        return None

    return base_dir / selected


def _reset_on_folder_change(current_folder: str) -> None:
    prev = st.session_state.get("dc_prev_folder")
    if prev != current_folder:
        st.session_state["dc_prev_folder"] = current_folder
