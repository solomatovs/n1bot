"""Вкладка загрузки — импорт страниц и пространств из Confluence в ChromaDB."""
from __future__ import annotations

from typing import Iterator

import streamlit as st

from errors import AppError
from load_pipeline.events import (
    ChunkingDone,
    ChunkProduced,
    LoadingDone,
    LoadPipelineEvent,
    PageFailed,
    PageLoaded,
    SectionChunked,
    SpaceEnumerated,
    StorageDone,
    StoreBatchDone,
    StoreBatchFailed,
)
from bootstrap import AppServices
from loaders import run_page_pipeline, run_space_pipeline
from ui.components import render_chunking_settings, render_page_id_settings, render_space_settings
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Загрузка данных из Confluence")

    tab_pages, tab_space = st.tabs(["По Page IDs", "По Space Key"])

    with tab_pages:
        _render_page_tab(services)

    with tab_space:
        _render_space_tab(services)


# ---------------------------------------------------------------------------
# Вкладка: загрузка по Page IDs
# ---------------------------------------------------------------------------

def _render_page_tab(services: AppServices) -> None:
    chunking_params = render_chunking_settings(key_prefix="pid_cp")
    storage_params = render_page_id_settings()

    st.subheader("Загрузка страниц")
    pids_input = st.text_input("Page IDs через запятую", key="pIds")
    col_name = st.text_input(
        "Collection name",
        key="pColName_classic",
        value=pids_input.replace(",", "_") if pids_input else "",
    )

    if not st.button("Загрузить и сохранить", key="btn_load_pages"):
        return

    page_ids = [x.strip() for x in pids_input.split(",") if x.strip()]
    if not page_ids:
        st.warning("Укажите хотя бы один Page ID.")
        return
    if not col_name:
        st.warning("Укажите имя коллекции.")
        return

    try:
        pipeline = run_page_pipeline(
            page_ids=page_ids,
            collection_name=col_name,
            services=services,
            chunking_params=chunking_params,
            storage_params=storage_params,
        )
        _consume_pipeline(pipeline)
        st.cache_data.clear()
    except AppError as e:
        st.error(f"Ошибка: {e}")


# ---------------------------------------------------------------------------
# Вкладка: загрузка пространства
# ---------------------------------------------------------------------------

def _render_space_tab(services: AppServices) -> None:
    chunking_params = render_chunking_settings(key_prefix="sp_cp")
    space_params, storage_params = render_space_settings()

    st.subheader("Загрузить пространство")
    space_key = st.text_input("Space Key", key="spaceKey_stream")
    col_name = st.text_input("Collection name", key="pCol_stream", value=space_key or "")

    if not st.button("Загрузить пространство", key="btn_load_space"):
        return

    if not space_key or not col_name:
        st.warning("Укажите Space Key и имя коллекции.")
        return

    try:
        pipeline = run_space_pipeline(
            space_key=space_key,
            collection_name=col_name,
            services=services,
            space_params=space_params,
            chunking_params=chunking_params,
            storage_params=storage_params,
        )
        _consume_pipeline(pipeline)
        st.cache_data.clear()
    except AppError as e:
        st.error(f"Ошибка: {e}")


# ---------------------------------------------------------------------------
# Единый обработчик потока событий
# ---------------------------------------------------------------------------

def _consume_pipeline(pipeline: Iterator[LoadPipelineEvent]) -> None:
    """Итерирует генератор пайплайна, обновляя Streamlit-виджеты по событиям."""
    with st.status("Загрузка...", expanded=True) as status:
        pbar = st.progress(0.0)
        msg = st.empty()

        for event in pipeline:
            match event:
                # -- Загрузка --
                case SpaceEnumerated(space_key=sk, total=n):
                    msg.write(f"Пространство «{sk}»: найдено {n} страниц")

                case PageLoaded(page_id=pid, index=i, total=t):
                    pbar.progress(i / t, text=f"{i}/{t} — загружена страница {pid}")

                case PageFailed(page_id=pid, index=i, total=t):
                    pbar.progress(i / t, text=f"{i}/{t} — ошибка: {pid}")

                case LoadingDone(ok_count=ok, failed_count=bad):
                    if ok == 0:
                        pbar.empty()
                        status.update(label="Ошибка", state="error")
                        st.error("Не удалось загрузить ни одной страницы.")
                        return
                    msg.write(f"Загружено: {ok} страниц, ошибок: {bad}")
                    pbar.progress(0.0, text="Чанкинг...")

                # -- Чанкинг --
                case SectionChunked(doc_index=di, doc_total=dt, section_index=si, section_total=st_, section_title=title):
                    pbar.progress(
                        di / dt,
                        text=f"Чанкинг {di}/{dt} — секция {si}/{st_}: {title}",
                    )

                case ChunkProduced():
                    pass

                case ChunkingDone(total_chunks=n):
                    msg.write(f"Получено {n} чанков. Сохраняю в ChromaDB...")
                    pbar.progress(0.0, text="Сохранение...")

                # -- Сохранение --
                case StoreBatchDone(total_stored=stored, total_chunks=total):
                    pbar.progress(
                        stored / max(total, 1),
                        text=f"Сохранено: {stored}/{total}",
                    )

                case StoreBatchFailed(error=err):
                    st.warning(f"Ошибка батча: {err}")

                case StorageDone(total_stored=stored, total_failed=bad):
                    pbar.empty()
                    status.update(label="Готово", state="complete")
                    msg.write(f"Готово. Сохранено: {stored}, ошибок: {bad}")
