"""Вкладка «Чат по документам» — вопросы по файлам из папки."""
from __future__ import annotations

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
)
from application.doc_pipeline.factory import create_doc_context, create_doc_pipeline
from domain.config import AppConfig
from domain.doc_chat import DocChatMessage, parse_messages, serialize_exchange
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

    history = MarkdownChatHistory(cfg.chat_history_path(folder_path))
    messages = history.load()

    _render_history(messages)

    user_prompt = st.chat_input("Введите ваш вопрос…", key="dc_chat_input")
    if not user_prompt:
        return

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
        answer = _consume_pipeline(pipeline.run(ctx))

    history.append(question=user_prompt, answer=answer)
    st.rerun()


# ---------------------------------------------------------------------------
# Потребитель pipeline — Streamlit-виджеты
# ---------------------------------------------------------------------------

def _consume_pipeline(events: Iterator[DocPipelineEvent]) -> str:
    """Итерирует события pipeline, обновляя UI. Возвращает итоговый ответ."""
    status_ph = st.empty()
    answer_ph = st.empty()
    answer_text = ""

    for event in events:
        match event:
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

            case SearchDone(hits=hits):
                if hits:
                    with st.expander(f"Найдено {len(hits)} фрагментов", expanded=False):
                        for hit in hits:
                            loc = hit.location
                            st.caption(f"**{loc.label}** (секция: {loc.section_title}, score: {hit.score:.2f})")
                            st.code(hit.content[:300], language="markdown")

            case ContextReady(context=_, sources=srcs):
                if srcs:
                    status_ph.caption(f"Контекст собран из: {', '.join(srcs)}")

            case AnswerToken(token=tok):
                answer_text += tok
                if answer_text.strip():
                    answer_ph.markdown(answer_text + "▌")

            case GenerationDone():
                status_ph.empty()
                if answer_text.strip():
                    answer_ph.markdown(answer_text)

    return answer_text


# ---------------------------------------------------------------------------
# Реализация ChatHistory — markdown-файл
# ---------------------------------------------------------------------------

class MarkdownChatHistory:
    """Хранилище истории чата в markdown-файле."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> List[DocChatMessage]:
        if not self._path.exists():
            return []
        text = self._path.read_text(encoding="utf-8")
        return parse_messages(text)

    def append(self, question: str, answer: str) -> None:
        block = serialize_exchange(question, answer)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(block)


# ---------------------------------------------------------------------------
# Выбор папки (без создания)
# ---------------------------------------------------------------------------

def _folder_selector_readonly(cfg: AppConfig) -> Path | None:
    """Selectbox с существующими папками. Без возможности создания."""
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
    """Сброс при смене папки."""
    prev = st.session_state.get("dc_prev_folder")
    if prev != current_folder:
        st.session_state["dc_prev_folder"] = current_folder


# ---------------------------------------------------------------------------
# Рендер истории
# ---------------------------------------------------------------------------

def _render_history(messages: List[DocChatMessage]) -> None:
    """Отрисовать историю чата."""
    for msg in messages:
        with st.chat_message(msg.role.value):
            st.markdown(msg.content)
