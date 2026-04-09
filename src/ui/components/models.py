"""Загрузка моделей LiteLLM — UI-слой с кэшированием."""
from __future__ import annotations

import logging
from typing import List

import httpx
import streamlit as st

from domain.config import AppConfig
from domain.models import filter_chat_models, filter_embedding_models
from ui.state import CacheTTL

log = logging.getLogger(__name__)


def extract_model_ids(response_data: List[dict]) -> List[str]:
    """Извлечь и отсортировать id моделей из ответа /v1/models."""
    return sorted(model_id for m in response_data if (model_id := m.get("id")))


@st.cache_data(ttl=CacheTTL.models, show_spinner=False)
def _fetch_models(models_url: str, auth_headers: dict[str, str], ssl_verify: bool) -> List[str]:
    """Получить список моделей через /v1/models."""
    try:
        resp = httpx.get(models_url, headers=auth_headers, verify=ssl_verify)
        resp.raise_for_status()
        return extract_model_ids(resp.json().get("data", []))
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Failed to fetch models: %s", e)
        return []


def get_all_models(cfg: AppConfig) -> List[str]:
    """Получить полный список моделей."""
    return _fetch_models(cfg.litellm_models_url, cfg.litellm_auth_headers, cfg.ssl_verify)


def get_chat_models(cfg: AppConfig) -> List[str]:
    """Получить список chat-моделей."""
    return filter_chat_models(get_all_models(cfg))


def get_embedding_models(cfg: AppConfig) -> List[str]:
    """Получить список embedding-моделей."""
    return filter_embedding_models(get_all_models(cfg))
