"""Загрузка и фильтрация моделей LiteLLM."""
from __future__ import annotations

import logging
from typing import List

import httpx
import streamlit as st

from domain.config import AppConfig
from ui.state import CacheTTL

log = logging.getLogger(__name__)

_EMBEDDING_KEYWORDS = ("embed", "e5", "bge", "gte", "instructor")


@st.cache_data(ttl=CacheTTL.models, show_spinner=False)
def _fetch_models(models_url: str, auth_headers: dict[str, str], ssl_verify: bool) -> List[str]:
    """Получить список моделей через /v1/models."""
    try:
        resp = httpx.get(models_url, headers=auth_headers, verify=ssl_verify)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return sorted(
            model_id
            for m in data
            if (model_id := m.get("id"))
        )
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Failed to fetch models: %s", e)
        return []


def get_all_models(cfg: AppConfig) -> List[str]:
    """Получить полный список моделей."""
    return _fetch_models(cfg.litellm_models_url, cfg.litellm_auth_headers, cfg.ssl_verify)


def is_embedding_model(model_id: str) -> bool:
    """Определить, является ли модель embedding-моделью по имени."""
    lower = model_id.lower()
    return any(kw in lower for kw in _EMBEDDING_KEYWORDS)


def get_chat_models(cfg: AppConfig) -> List[str]:
    """Получить список chat-моделей."""
    return [m for m in get_all_models(cfg) if not is_embedding_model(m)]


def get_embedding_models(cfg: AppConfig) -> List[str]:
    """Получить список embedding-моделей."""
    return [m for m in get_all_models(cfg) if is_embedding_model(m)]
