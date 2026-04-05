"""Вкладка «Векторное хранилище» — просмотр и управление коллекциями."""
from __future__ import annotations

import streamlit as st

from ui.components import (
    fetch_collection_df,
    get_collection_preview,
    list_collections,
)
from ui.state import AppConfig, SessionState
from vectorstore import remove_collection


def render(cfg: AppConfig, state: SessionState) -> None:
    st.title("Векторное хранилище")

    colls = list_collections(cfg.chroma_db_path)
    if colls and state.selected_collection not in colls:
        state.selected_collection = colls[0]

    state.selected_collection = st.selectbox(
        "Коллекция",
        colls or [state.selected_collection],
        key="select_collection_data",
    ) or state.selected_collection

    show_full = st.toggle("Показывать полный состав коллекции", value=False)
    if show_full:
        with st.spinner("Гружу полный состав…"):
            _show_full_collection(cfg, state.selected_collection)
    else:
        st.caption("Превью (сокращено до 200 символов):")
        st.dataframe(
            get_collection_preview(cfg.chroma_db_path, state.selected_collection),
            height=400,
        )

    if st.button(f"Удалить коллекцию «{state.selected_collection}»", type="primary"):
        try:
            remove_collection(state.selected_collection, db_path=cfg.chroma_db_path)
            st.success("Коллекция удалена.")
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Не удалось удалить: {e}")


def _show_full_collection(cfg: AppConfig, name: str) -> None:
    try:
        df = fetch_collection_df(cfg.chroma_db_path, name, preview=False)
        st.success(f"Документов: {len(df)} в «{name}»")
        st.dataframe(df, height=500)
    except Exception as e:
        st.error(f"Ошибка при получении коллекции: {e}")
