"""Провайдер fastembed: локальные эмбеддинги in-process."""

from __future__ import annotations

from boba.llm.fastembed.embedding import (
    MANIFEST,
    FastembedBackend,
    FastembedEmbeddingModel,
    FastembedProvider,
)

__all__ = [
    "MANIFEST",
    "FastembedBackend",
    "FastembedEmbeddingModel",
    "FastembedProvider",
]
