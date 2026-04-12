"""Работа с коллекциями векторного хранилища в UI."""
from __future__ import annotations

import logging
from typing import List

import pandas as pd
import streamlit as st

from domain.errors import VectorStoreError
from domain.core.vectorstore import VectorStoreService

log = logging.getLogger(__name__)

_EMPTY_DF = pd.DataFrame({"id": [], "text": [], "metadata": []})


def list_collection_names(vs: VectorStoreService) -> List[str]:
    """Получить список имён коллекций. При ошибке — пустой список с предупреждением."""
    try:
        return [c.name for c in vs.list_collections()]
    except VectorStoreError as ex:
        log.warning("Failed to list collections: %s", ex)
        st.warning(f"Не удалось получить список коллекций: {ex}")
        return []


def collection_to_dataframe(vs: VectorStoreService, collection_name: str) -> pd.DataFrame:
    """Получить содержимое коллекции как DataFrame."""
    data = vs.get_collection_data(collection_name)
    return pd.DataFrame({
        "id": data.ids,
        "text": data.documents,
        "metadata": data.metadatas,
    })


def collection_preview(vs: VectorStoreService, collection_name: str) -> pd.DataFrame:
    """Получить превью коллекции. При ошибке — пустой DataFrame с сообщением."""
    try:
        df = collection_to_dataframe(vs, collection_name)
        df["text"] = df["text"].astype(str)
        return df
    except VectorStoreError as e:
        log.warning("Failed to load collection data: %s", e)
        st.error(f"Ошибка загрузки данных: {e}")
        return _EMPTY_DF
