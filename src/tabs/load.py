"""Вкладка загрузки — импорт страниц и пространств из Confluence в ChromaDB."""
from __future__ import annotations

import streamlit as st
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat

from chunking import split_into_chunks_semantic
from config import enc
from confluence import ingest_space_incremental
from errors import AppError
from ui.state import AppConfig, SessionState
from vectorstore import VectorStoreService


def render(cfg: AppConfig, state: SessionState) -> None:
    st.title("Загрузка данных из Confluence")

    source_type = st.radio("Источник", ["pageIds", "spaceKey"], horizontal=True, key="classic_src")

    if source_type == "spaceKey":
        _render_space_loader(cfg)
    elif source_type == "pageIds":
        _render_page_loader(cfg)


# ---------------------------------------------------------------------------
# Загрузка пространства
# ---------------------------------------------------------------------------

def _render_space_loader(cfg: AppConfig) -> None:
    st.subheader("Загрузить пространство")
    space_key = st.text_input("Space Key", key="spaceKey_stream")
    col_name = st.text_input("Collection name", key="pCol_stream", value=space_key or "")
    summarize = st.checkbox("Делать резюме", value=False)

    if not st.button("Загрузить пространство"):
        return

    if not space_key or not col_name:
        st.warning("Укажите Space Key и имя коллекции.")
        return

    try:
        vs_service = VectorStoreService(cfg)
        result = ingest_space_incremental(
            vs_service=vs_service,
            base_url=cfg.confluence_url,
            token=cfg.confluence_token,
            space_key=space_key,
            collection_name=col_name,
            ollama_api_url=cfg.litellm_url,
            summarize=bool(summarize),
            verify_ssl=False,
        )
        st.success(
            f"Готово. Обработано страниц: {result.processed_pages}. "
            f"Успешно добавлено документов: {result.ok_docs}, ошибок: {result.bad_docs}"
        )
        st.cache_data.clear()
    except AppError as e:
        st.error(f"Ошибка загрузки: {e}")


# ---------------------------------------------------------------------------
# Загрузка страниц
# ---------------------------------------------------------------------------

def _render_page_loader(cfg: AppConfig) -> None:
    st.subheader("Загрузка страницы")

    pids = st.text_input("Page IDs через запятую", key="pIds")
    col_name = st.text_input(
        "Collection name для загрузки",
        key="pColName_classic",
        value=pids.replace(",", "_") if pids else "",
    )

    if st.button("Загрузить страницы"):
        loader = ConfluenceLoader(
            url=cfg.confluence_url,
            token=cfg.confluence_token,
            include_attachments=False,
            keep_markdown_format=True,
            content_format=ContentFormat.EXPORT_VIEW,
            page_ids=[x.strip() for x in pids.split(",") if x.strip()],
            confluence_kwargs={"verify_ssl": False},
            limit=50,
        )
        docs = loader.load()
        st.session_state["confl_docs"] = docs
        st.success(f"Загружено страниц: {len(docs)}")

    if st.button("Сохранить в ChromaDB"):
        docs = st.session_state.get("confl_docs", [])
        if not docs:
            st.warning("Сначала загрузите документы.")
        elif not col_name:
            st.warning("Укажите имя коллекции.")
        else:
            vs_service = VectorStoreService(cfg)
            chunks = split_into_chunks_semantic(docs, ollama_api_url=cfg.litellm_url, tokenizer=enc)
            store = vs_service.store_documents(
                chunks,
                collection_name=col_name,
                batch_size=32,
            )
            if store is not None:
                store_data = store.get()
                st.success(f"Документов в коллекции: {len(store_data['documents'] or [])}")
            st.cache_data.clear()
