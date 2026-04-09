"""Селекторы — выпадающие списки для выбора коллекции, модели и т.д."""
from __future__ import annotations

from typing import List

import streamlit as st

from domain.config import AppConfig
from domain.vectorstore import VectorStoreService
from ui.components.collections import list_collection_names
from ui.components.models import get_chat_models, get_embedding_models
from ui.components.utils import safe_index


def collection_selector(vs: VectorStoreService, *, key: str, current: str) -> str:
    """Селектор коллекции."""
    colls = list_collection_names(vs)
    if not colls:
        st.warning("Нет доступных коллекций.")
        return current
    index = safe_index(colls, current)
    selected = st.selectbox("Имя векторной БД (коллекция)", colls, index=index, key=key)
    return selected if selected is not None else colls[0]


def model_selector(cfg: AppConfig, *, key: str | None = None) -> str:
    """Селектор модели генерации."""
    return _model_selectbox(
        label="Модель генерации",
        models=get_chat_models(cfg),
        default=cfg.default_model,
        empty_warning="Нет доступных моделей генерации.",
        key=key,
    )


def embedding_model_selector(cfg: AppConfig, *, key: str = "embedding_model") -> str:
    """Селектор embedding модели."""
    return _model_selectbox(
        label="Embedding модель",
        models=get_embedding_models(cfg),
        default=cfg.embedding_model,
        empty_warning="Нет доступных embedding моделей.",
        key=key,
    )


def _model_selectbox(
    label: str,
    models: List[str],
    default: str,
    empty_warning: str,
    key: str | None = None,
) -> str:
    """Общий паттерн для селектора модели."""
    if not models:
        st.warning(empty_warning)
        return default
    index = safe_index(models, default)
    selected = st.selectbox(label, models, index=index, key=key)
    return selected if selected is not None else models[0]
