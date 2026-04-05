"""Переиспользуемые UI-компоненты Streamlit."""
from __future__ import annotations

import re
from typing import List

import pandas as pd
import requests
import streamlit as st

import chromadb
from chromadb.config import Settings

from ui.state import AppConfig, ChatMessage


# ---------------------------------------------------------------------------
# Кэшируемые ресурсы
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_chroma_client(db_path: str):  # -> chromadb.PersistentClient
    return chromadb.PersistentClient(
        path=db_path,
        settings=Settings(anonymized_telemetry=False),
    )


@st.cache_data(ttl=60, show_spinner=False)
def list_collections(db_path: str) -> List[str]:
    try:
        return [c.name for c in get_chroma_client(db_path).list_collections()]
    except Exception as ex:
        st.warning(f"Не удалось получить список коллекций: {ex}")
        return []


@st.cache_data(ttl=60, show_spinner=False)
def get_openai_models(litellm_url: str, api_key: str) -> List[str]:
    try:
        resp = requests.get(
            f"{litellm_url.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json()["data"])
    except Exception as e:
        st.error(f"Ошибка получения моделей: {e}")
        return []


# ---------------------------------------------------------------------------
# Помощники для коллекций
# ---------------------------------------------------------------------------

def fetch_collection_df(
    db_path: str,
    collection_name: str,
    preview: bool = False,
) -> pd.DataFrame:
    coll = get_chroma_client(db_path).get_collection(collection_name)
    data = coll.get(include=["documents", "metadatas"])
    ids = data.get("ids", [])
    docs = data.get("documents", []) or []
    metas = data.get("metadatas", []) or []
    if preview:
        docs = [d if isinstance(d, str) else str(d) for d in docs]
    return pd.DataFrame({"id": ids, "text": docs, "metadata": metas})


@st.cache_data(ttl=20, show_spinner=False)
def get_collection_preview(db_path: str, collection_name: str) -> pd.DataFrame:
    try:
        return fetch_collection_df(db_path, collection_name, preview=True)
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}")
        return pd.DataFrame({"id": [], "text": [], "metadata": []})


# ---------------------------------------------------------------------------
# Селекторы
# ---------------------------------------------------------------------------

def collection_selector(cfg: AppConfig, *, key: str, current: str) -> str:
    """Отрисовать селектор коллекции и вернуть выбранное значение."""
    colls = list_collections(cfg.chroma_db_path)
    index = colls.index(current) if current in colls else 0
    return st.selectbox(
        "Имя векторной БД (коллекция)",
        colls or [current],
        index=index,
        key=key,
    ) or current


def model_selector(cfg: AppConfig) -> str:
    """Отрисовать селектор модели и вернуть выбранное значение."""
    models = get_openai_models(cfg.litellm_url, cfg.litellm_api_key)
    default_idx = models.index(cfg.default_model) if cfg.default_model in models else 0
    return st.selectbox("Модель генерации", models, index=default_idx) or cfg.default_model


# ---------------------------------------------------------------------------
# Рендер истории чата
# ---------------------------------------------------------------------------

def render_chat_history(history: List[ChatMessage]) -> None:
    """Отрисовать историю чата с раскрывающимися блоками контекста и размышлений."""
    for msg in history:
        with st.chat_message("user"):
            st.markdown(msg.question)
        with st.chat_message("assistant"):
            if msg.rag_context:
                with st.expander("Найденный контекст из базы знаний", expanded=False):
                    st.markdown(msg.rag_context)
            if msg.thinking:
                with st.expander("Процесс размышления", expanded=False):
                    st.markdown(msg.thinking)
            st.markdown(msg.answer)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def extract_page_ids_from_answer(text: str) -> set[str]:
    return set(re.findall(r"-\s+[^\s:]+:(\d+)\b", text))
