"""Доменная логика классификации моделей."""

from __future__ import annotations

from typing import List

_EMBEDDING_KEYWORDS = ("embed", "e5", "bge", "gte", "instructor")


def is_embedding_model(model_id: str) -> bool:
    """Определить, является ли модель embedding-моделью по имени."""
    lower = model_id.lower()
    return any(kw in lower for kw in _EMBEDDING_KEYWORDS)


def filter_chat_models(models: List[str]) -> List[str]:
    """Отфильтровать chat-модели (не embedding)."""
    return [m for m in models if not is_embedding_model(m)]


def filter_embedding_models(models: List[str]) -> List[str]:
    """Отфильтровать embedding-модели."""
    return [m for m in models if is_embedding_model(m)]
