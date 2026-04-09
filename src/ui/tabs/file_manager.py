"""Вкладка файлового менеджера — просмотр, редактирование, удаление импортированных файлов."""
from __future__ import annotations

from pathlib import Path
from typing import List

import streamlit as st

from infrastructure.bootstrap import AppServices
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Импортированные документы")

    base_dir = Path(services.cfg.import_base_dir)
    if not base_dir.exists():
        st.info(f"Директория `{base_dir}` не существует. Импортируйте документы из Confluence.")
        return

    folders = _list_folders(base_dir)
    if not folders:
        st.info("Нет импортированных папок.")
        return

    selected_folder = st.selectbox("Папка", folders, key="fm_folder")
    if not selected_folder:
        return

    folder_path = base_dir / selected_folder

    _render_upload(folder_path)
    _render_file_list(folder_path)


# ---------------------------------------------------------------------------
# Список файлов
# ---------------------------------------------------------------------------

def _render_file_list(folder_path: Path) -> None:
    files = _list_files(folder_path)
    if not files:
        st.info("Папка пуста.")
        return

    # Проверяем, есть ли открытый файл в session_state
    opened_file = st.session_state.get("fm_opened_file")

    if opened_file and (folder_path / opened_file).exists():
        _render_file_editor(folder_path, opened_file)
        return

    # Список файлов
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

    # Удалить все
    st.divider()
    if st.button("Удалить все файлы", key="fm_delete_all", type="primary"):
        for filename in files:
            (folder_path / filename).unlink(missing_ok=True)
        st.rerun()


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
# Добавление файла
# ---------------------------------------------------------------------------

def _render_upload(folder_path: Path) -> None:
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
# Утилиты
# ---------------------------------------------------------------------------

def _list_folders(base_dir: Path) -> List[str]:
    """Список подпапок в базовой директории."""
    if not base_dir.is_dir():
        return []
    return sorted(d.name for d in base_dir.iterdir() if d.is_dir())


def _list_files(folder_path: Path) -> List[str]:
    """Список файлов в папке."""
    if not folder_path.is_dir():
        return []
    return sorted(f.name for f in folder_path.iterdir() if f.is_file())
