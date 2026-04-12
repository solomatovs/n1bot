"""Получение списка доступных моделей из LiteLLM."""

from __future__ import annotations

import logging
from typing import List

import httpx

from boba_domain.config import AppConfig
from boba_domain.models import filter_chat_models

log = logging.getLogger(__name__)


def fetch_chat_models(cfg: AppConfig) -> List[str]:
    """Запросить LiteLLM /v1/models, вернуть отсортированные chat-модели."""
    try:
        r = httpx.get(
            cfg.litellm_models_url,
            headers=cfg.litellm_auth_headers,
            verify=cfg.ssl_verify,
            timeout=10,
        )
        r.raise_for_status()
        all_models = sorted(m["id"] for m in r.json().get("data", []))
        return filter_chat_models(all_models)
    except Exception as e:
        log.warning("Не удалось получить список моделей: %s", e)
        return []
