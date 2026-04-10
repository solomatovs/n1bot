"""Вкладка файлового менеджера — просмотр, редактирование, удаление импортированных файлов."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import streamlit as st

from domain.convert import (
    ConvertDone,
    ConvertEvent,
    ConvertFileDone,
    ConvertFileFailed,
    ConvertFileStarted,
)
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

    _render_upload(folder_path)
    _render_convert_button(folder_path, services)
    _render_file_list(folder_path)


def _reset_opened_file_on_folder_change(current_folder: str) -> None:
    """Сбросить открытый файл при смене папки."""
    prev_folder = st.session_state.get("fm_prev_folder")
    if prev_folder != current_folder:
        st.session_state.pop("fm_opened_file", None)
        st.session_state["fm_prev_folder"] = current_folder


# ---------------------------------------------------------------------------
# Загрузка файлов
# ---------------------------------------------------------------------------

def _render_upload(folder_path: Path) -> None:
    """file_uploader с автосохранением и очисткой."""
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
    st.rerun()


# ---------------------------------------------------------------------------
# Конвертация в Markdown
# ---------------------------------------------------------------------------

def _render_convert_button(folder_path: Path, services: AppServices) -> None:
    """Кнопка подготовки документов для индексации."""
    if not st.button("Индексировать", key="fm_convert"):
        return

    output_dir = services.cfg.context_path(folder_path)
    _consume_convert_events(services.document_preparer.prepare_folder(folder_path, output_dir))


def _consume_convert_events(events: Iterator[ConvertEvent]) -> None:
    """Обработка событий конвертации с прогресс-баром."""
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
                        status.update(label="Готово", state="complete")
                        msg.write(f"Конвертация завершена. Успешно: {ok}, ошибок: {bad}.")


# ---------------------------------------------------------------------------
# Список файлов
# ---------------------------------------------------------------------------

def _render_file_list(folder_path: Path) -> None:
    """Список файлов или редактор открытого файла."""
    files = _list_files(folder_path)
    if not files:
        st.info("Папка пуста.")
        return

    opened_file = st.session_state.get("fm_opened_file")
    if opened_file and (folder_path / opened_file).exists():
        _render_file_editor(folder_path, opened_file)
        return

    _render_file_rows(folder_path, files)

    st.divider()
    if st.button("Удалить все файлы", key="fm_delete_all", type="primary"):
        for filename in files:
            (folder_path / filename).unlink(missing_ok=True)
        st.rerun()


def _render_file_rows(folder_path: Path, files: List[str]) -> None:
    """Строки файлов: имя-ссылка, размер, удаление."""
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
                st.rerun()


# ---------------------------------------------------------------------------
# Редактор файла
# ---------------------------------------------------------------------------

def _render_file_editor(folder_path: Path, filename: str) -> None:
    """Просмотр и редактирование содержимого файла."""
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
    """Список файлов в папке (скрытые директории вроде .boba игнорируются)."""
    if not folder_path.is_dir():
        return []
    return sorted(
        f.name for f in folder_path.iterdir()
        if f.is_file() and not f.name.startswith(".")
    )
