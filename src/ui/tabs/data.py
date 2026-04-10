"""Вкладка «Векторное хранилище» — просмотр и управление per-folder коллекциями."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from domain.errors import VectorStoreError
from infrastructure.bootstrap import AppServices
from ui.components.collections import (
    collection_preview,
    collection_to_dataframe,
    list_collection_names,
)
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Векторное хранилище")

    cfg = services.cfg
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name for d in base_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not folders:
        st.info("Нет папок с документами. Импортируйте документы во вкладке «Документы».")
        return

    selected = st.selectbox("Папка с документами", folders, key="vs_folder")
    if not selected:
        return

    folder_path = base_dir / selected
    chroma_path = str(cfg.chroma_path(folder_path))
    collection_name = cfg.collection_name(selected)

    # Создаём vectorstore для выбранной папки
    vs = services.create_vectorstore(chroma_path)

    colls = list_collection_names(vs)
    if not colls:
        st.info(
            f"В папке «{selected}» нет векторной базы. "
            "Векторный индекс создаётся автоматически при первом вопросе "
            "во вкладке «Чат по документам»."
        )
        return

    st.caption(f"Коллекция: **{collection_name}**")

    emb_model = vs.get_collection_embedding_model(collection_name)
    st.caption(f"Embedding модель: **{emb_model or 'не задана'}**")

    show_full = st.toggle("Показывать полный состав коллекции", value=False, key="vs_full")
    if show_full:
        with st.spinner("Гружу полный состав…"):
            _show_full_collection(vs, collection_name)
    else:
        st.caption("Превью (сокращено до 200 символов):")
        st.dataframe(
            collection_preview(vs, collection_name),
            height=400,
        )

    if st.button(f"Удалить коллекцию «{collection_name}»", type="primary", key="vs_delete"):
        try:
            vs.remove_collection(collection_name)
            st.success("Коллекция удалена.")
            st.rerun()
        except VectorStoreError as e:
            st.error(f"Не удалось удалить: {e}")


def _show_full_collection(vs, name: str) -> None:
    try:
        df = collection_to_dataframe(vs, name)
        st.success(f"Документов: {len(df)} в «{name}»")
        st.dataframe(df, height=500)
    except VectorStoreError as e:
        st.error(f"Ошибка при получении коллекции: {e}")
