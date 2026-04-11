"""Селекторы — выпадающие списки для выбора модели и т.д."""
from __future__ import annotations

from typing import List

import streamlit as st

from domain.config import AppConfig
from ui.components.models import get_chat_models
from ui.components.utils import safe_index


def model_selector(cfg: AppConfig, *, key: str | None = None) -> str:
    """Селектор модели генерации."""
    return _model_selectbox(
        label="Модель генерации",
        models=get_chat_models(cfg),
        default=cfg.default_model,
        empty_warning="Нет доступных моделей генерации.",
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
