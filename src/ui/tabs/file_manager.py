"""Вкладка файлового менеджера — просмотр, редактирование, удаление импортированных файлов.

Любое изменение документов (загрузка, удаление, ручная переиндексация)
запускает единый процесс: конвертация HTML→Markdown → индексация в векторную базу.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import streamlit as st

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import (
    DocPipelineEvent,
    FileIndexed,
    IndexingDone,
    IndexingSkipped,
)
from application.doc_pipeline.factory import create_doc_context, create_index_pipeline
from domain.convert import (
    ConvertDone,
    ConvertEvent,
    ConvertFileDone,
    ConvertFileFailed,
    ConvertFileStarted,
)
from domain.pipeline import StageCompleted, StageStarted
from infrastructure.bootstrap import AppServices
from ui.components.folder_selector import folder_selector
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Импортированные документы")

    base_dir = Path(services.cfg.import_base_dir)
    folder_path = folder_selector(base_dir, key_prefix="fm")

    if folder_path is None:
        return

    _reset_opened_file_on_folder_change(folder_path.name)

    _render_upload(folder_path, services)
    _render_reindex_button(folder_path, services)
    _render_file_list(folder_path, services)


def _reset_opened_file_on_folder_change(current_folder: str) -> None:
    prev_folder = st.session_state.get("fm_prev_folder")
    if prev_folder != current_folder:
        st.session_state.pop("fm_opened_file", None)
        st.session_state["fm_prev_folder"] = current_folder


# ---------------------------------------------------------------------------
# Единый процесс: конвертация + индексация
# ---------------------------------------------------------------------------

def _convert_and_index(folder_path: Path, services: AppServices) -> None:
    """Конвертировать документы и переиндексировать векторную базу."""
    output_dir = services.cfg.context_path(folder_path)

    # Шаг 1: конвертация HTML → Markdown
    _consume_convert_events(services.document_preparer.prepare_folder(folder_path, output_dir))

    # Шаг 2: индексация в векторную базу
    ctx = create_doc_context(
        folder_path=folder_path,
        query="",
        model="",
        services=services,
    )
    pipeline = create_index_pipeline()
    _consume_index_events(pipeline.run(ctx))


def _consume_convert_events(events: Iterator[ConvertEvent]) -> None:
    with st.status("Конвертация...", expanded=True) as status:
        pbar = st.progress(0.0)
        msg = st.empty()

        for event in events:
            match event:
                case ConvertFileStarted(filename=name, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — {name}")

                case ConvertFileDone(source=src, target=tgt, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — {src} → {tgt}")

                case ConvertFileFailed(filename=name, error=err, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — ошибка: {name}")
                    st.warning(f"Ошибка {name}: {err}")

                case ConvertDone(ok_count=ok, failed_count=bad):
                    pbar.empty()
                    if ok == 0 and bad == 0:
                        status.update(label="Нет файлов для конвертации", state="complete")
                        msg.info("В папке нет HTML-файлов (.html, .htm).")
                    else:
                        status.update(label="Конвертация завершена", state="complete")
                        msg.write(f"Конвертация: успешно {ok}, ошибок: {bad}.")


def _consume_index_events(events: Iterator[DocPipelineEvent]) -> None:
    with st.status("Индексация...", expanded=True) as status:
        pbar = st.progress(0.0)
        msg = st.empty()

        for event in events:
            match event:
                case StageStarted():
                    msg.write("Проверка актуальности индекса...")

                case IndexingSkipped(collection=name, doc_count=n):
                    pbar.empty()
                    status.update(label="Индекс актуален", state="complete")
                    msg.write(f"Коллекция «{name}»: {n} чанков, изменений нет.")

                case FileIndexed(filename=name, chunks=c, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — {name} ({c} чанков)")

                case IndexingDone(total_files=f, total_chunks=c):
                    pbar.empty()
                    status.update(label="Индексация завершена", state="complete")
                    msg.write(f"Проиндексировано: {f} файлов, {c} чанков.")

                case StageCompleted():
                    pass


# ---------------------------------------------------------------------------
# Загрузка файлов → автоматическая конвертация + индексация
# ---------------------------------------------------------------------------

def _render_upload(folder_path: Path, services: AppServices) -> None:
    upload_key = st.session_state.get("fm_upload_key", 0)

    uploaded = st.file_uploader(
        "Загрузить файлы",
        accept_multiple_files=True,
        key=f"fm_upload_{upload_key}",
    )
    if not uploaded:
        return

    for f in uploaded:
        (folder_path / f.name).write_bytes(f.getvalue())

    st.success(f"Сохранено: {len(uploaded)} файлов")
    st.session_state["fm_upload_key"] = upload_key + 1

    _convert_and_index(folder_path, services)


# ---------------------------------------------------------------------------
# Ручная переиндексация
# ---------------------------------------------------------------------------

def _render_reindex_button(folder_path: Path, services: AppServices) -> None:
    if not st.button("Переиндексировать", key="fm_reindex"):
        return

    _convert_and_index(folder_path, services)


# ---------------------------------------------------------------------------
# Список файлов
# ---------------------------------------------------------------------------

def _render_file_list(folder_path: Path, services: AppServices) -> None:
    files = _list_files(folder_path)
    if not files:
        st.info("Папка пуста.")
        return

    opened_file = st.session_state.get("fm_opened_file")
    if opened_file and (folder_path / opened_file).exists():
        _render_file_editor(folder_path, opened_file)
        return

    _render_file_rows(folder_path, files, services)

    st.divider()
    if st.button("Удалить все файлы", key="fm_delete_all", type="primary"):
        for filename in files:
            (folder_path / filename).unlink(missing_ok=True)
        _convert_and_index(folder_path, services)


def _render_file_rows(
    folder_path: Path, files: List[str], services: AppServices,
) -> None:
    for filename in files:
        file_path = folder_path / filename
        size_kb = file_path.stat().st_size / 1024

        col_name, col_size, col_delete = st.columns([5, 1, 1])
        with col_name:
            if st.button(filename, key=f"fm_open_{filename}", type="tertiary"):
                st.session_state["fm_opened_file"] = filename
                st.rerun()
        with col_size:
            st.caption(f"{size_kb:.1f} КБ")
        with col_delete:
            if st.button("Удалить", key=f"fm_del_{filename}"):
                file_path.unlink()
                _convert_and_index(folder_path, services)


# ---------------------------------------------------------------------------
# Редактор файла
# ---------------------------------------------------------------------------

def _render_file_editor(folder_path: Path, filename: str) -> None:
    file_path = folder_path / filename

    if st.button("← Назад к списку", key="fm_back"):
        st.session_state.pop("fm_opened_file", None)
        st.rerun()

    st.subheader(filename)
    size_kb = file_path.stat().st_size / 1024
    st.caption(f"Размер: {size_kb:.1f} КБ")

    content = file_path.read_text(encoding="utf-8", errors="replace")

    edited = st.text_area(
        "Содержимое",
        value=content,
        height=500,
        key="fm_editor",
        label_visibility="collapsed",
    )

    col_save, col_delete = st.columns([1, 1])
    with col_save:
        if st.button("Сохранить", key="fm_save"):
            file_path.write_text(edited, encoding="utf-8")
            st.success("Сохранено")
    with col_delete:
        if st.button("Удалить файл", key="fm_delete_open", type="primary"):
            file_path.unlink()
            st.session_state.pop("fm_opened_file", None)
            st.rerun()


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _list_files(folder_path: Path) -> List[str]:
    if not folder_path.is_dir():
        return []
    return sorted(
        f.name for f in folder_path.iterdir()
        if f.is_file() and not f.name.startswith(".")
    )
