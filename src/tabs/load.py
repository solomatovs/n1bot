"""Вкладка загрузки — импорт страниц и пространств из Confluence в ChromaDB."""
from __future__ import annotations

import streamlit as st

from chunking import AdvancedChunker
from errors import AppError
from loaders import (
    BatchPageLoader,
    IngestionService,
    PageLoader,
    SpaceLoader,
)
from ui.components import render_chunking_settings, render_page_id_settings, render_space_settings
from ui.state import (
    AppConfig,
    ChunkingParams,
    SessionState,
    SpaceLoadParams,
)
from vectorstore import VectorStoreService


def render(cfg: AppConfig, state: SessionState) -> None:
    st.title("Загрузка данных из Confluence")

    tab_pages, tab_space = st.tabs(["По Page IDs", "По Space Key"])

    with tab_pages:
        _render_page_tab(cfg)

    with tab_space:
        _render_space_tab(cfg)


# ---------------------------------------------------------------------------
# Вкладка: загрузка по Page IDs
# ---------------------------------------------------------------------------

def _render_page_tab(cfg: AppConfig) -> None:
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
        with st.status("Загрузка...", expanded=True) as status:
            st.write(f"Загружаю {len(page_ids)} страниц из Confluence...")
            loader = _create_batch_loader(cfg)
            progress = StreamlitProgress()
            batch_result = loader.load(page_ids, progress)

            if not batch_result.loaded:
                status.update(label="Ошибка", state="error")
                st.error("Не удалось загрузить ни одной страницы.")
                return

            st.write(f"Загружено: {batch_result.ok_count} страниц. Разбиваю на чанки...")
            ingestion = _create_ingestion_service(cfg, chunking_params)
            chunk_progress = StreamlitProgress("Чанкинг...")
            chunks = ingestion.chunk(batch_result.loaded, chunk_progress)

            st.write(f"Получено {len(chunks)} чанков. Сохраняю в ChromaDB...")
            result = ingestion.store(chunks, col_name, storage_params)

            status.update(label="Готово", state="complete")
            st.write(f"Сохранено в коллекцию «{col_name}»: {result.chunks_stored} документов")

        st.cache_data.clear()
    except AppError as e:
        st.error(f"Ошибка: {e}")


# ---------------------------------------------------------------------------
# Вкладка: загрузка пространства
# ---------------------------------------------------------------------------

def _render_space_tab(cfg: AppConfig) -> None:
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
        with st.status("Загрузка пространства...", expanded=True) as status:
            st.write(f"Получаю список страниц пространства «{space_key}»...")
            space_loader = _create_space_loader(cfg, space_params)
            progress = StreamlitProgress()
            batch_result = space_loader.load(space_key, progress)

            if not batch_result.loaded:
                status.update(label="Ошибка", state="error")
                st.error("Не удалось загрузить ни одной страницы из пространства.")
                return

            st.write(
                f"Загружено: {batch_result.ok_count} страниц, "
                f"ошибок: {batch_result.failed_count}. Разбиваю на чанки..."
            )
            ingestion = _create_ingestion_service(cfg, chunking_params)
            chunk_progress = StreamlitProgress("Чанкинг...")
            chunks = ingestion.chunk(batch_result.loaded, chunk_progress)
            st.write(f"Получено {len(chunks)} чанков. Сохраняю в ChromaDB...")
            result = ingestion.store(chunks, col_name, storage_params)

            status.update(label="Готово", state="complete")
            st.write(f"Сохранено в коллекцию «{col_name}»: {result.chunks_stored} документов")

        st.cache_data.clear()
    except AppError as e:
        st.error(f"Ошибка: {e}")


# ---------------------------------------------------------------------------
# Streamlit-реализация ProgressCallback
# ---------------------------------------------------------------------------

class StreamlitProgress:
    """Реализация ProgressCallback через st.progress."""

    def __init__(self, label: str = "Начинаю…") -> None:
        self._pbar = st.progress(0.0, text=label)

    def on_step(self, index: int, total: int, label: str) -> None:
        self._pbar.progress(
            index / max(total, 1),
            text=f"{index}/{total} — {label}",
        )

    def on_complete(self) -> None:
        self._pbar.empty()


# ---------------------------------------------------------------------------
# Фабрики сервисов
# ---------------------------------------------------------------------------

def _create_batch_loader(cfg: AppConfig) -> BatchPageLoader:
    return BatchPageLoader(PageLoader(cfg))


def _create_space_loader(cfg: AppConfig, space_params: SpaceLoadParams) -> SpaceLoader:
    batch = _create_batch_loader(cfg)
    return SpaceLoader(batch, cfg, space_params)


def _create_ingestion_service(cfg: AppConfig, chunking_params: ChunkingParams) -> IngestionService:
    return IngestionService(
        chunker=AdvancedChunker(cfg, chunking_params),
        vs_service=VectorStoreService(cfg),
    )
