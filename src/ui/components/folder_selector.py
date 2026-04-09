"""Компонент выбора папки с возможностью создания новой."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import streamlit as st

_CREATE_OPTION = "[ + Создать папку ]"


def folder_selector(base_dir: Path, key_prefix: str = "fs") -> Optional[Path]:
    """Selectbox с папками + создание новой. Возвращает Path выбранной папки или None."""
    base_dir.mkdir(parents=True, exist_ok=True)
    p = key_prefix
    select_key = f"{p}_folder_select"
    pending_key = f"{p}_pending_folder"

    # Если только что создали папку — выбираем её
    pending = st.session_state.pop(pending_key, None)
    if pending:
        st.session_state[select_key] = pending

    folders = _list_folders(base_dir)
    options = folders + [_CREATE_OPTION]
    selected = st.selectbox("Папка", options, key=select_key)

    if selected == _CREATE_OPTION:
        _render_create_folder(base_dir, key_prefix=p, pending_key=pending_key)
        return None

    if not selected:
        return None

    return base_dir / selected


def _render_create_folder(base_dir: Path, key_prefix: str, pending_key: str) -> None:
    """Форма создания новой папки."""
    p = key_prefix

    name = st.text_input("Имя папки", key=f"{p}_new_folder_name")

    if not st.button("Создать", key=f"{p}_create_folder"):
        return

    name = name.strip()
    if not name:
        st.warning("Введите имя папки.")
        return

    new_path = base_dir / name
    if new_path.exists():
        st.warning("Папка уже существует.")
        return

    new_path.mkdir(parents=True)
    st.session_state[pending_key] = name
    st.success(f"Папка «{name}» создана")
    st.rerun()


def _list_folders(base_dir: Path) -> List[str]:
    """Список подпапок в базовой директории."""
    if not base_dir.is_dir():
        return []
    return sorted(d.name for d in base_dir.iterdir() if d.is_dir())
