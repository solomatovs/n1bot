"""Единая точка работы с LLM-провайдерами: транспорт, чат, эмбеддинги.

Чат-модель (`boba.llm.chat`) импортируется напрямую: она тянет langchain,
которого нет в песочных payload-окружениях.

Ошибки: см. docstring'и модулей пакета.
"""

from boba.llm.embedding import (
    EmbedderFactory,
    EmbeddingConfig,
    EmbeddingError,
    LocalEmbedding,
    LocalFastEmbedEmbedder,
    OpenAiEmbedder,
    OpenAiEmbedding,
)
from boba.llm.openai import OpenAiConfig, OpenAiDumpConfig, OpenAiHttp

__all__ = [
    "EmbedderFactory",
    "EmbeddingConfig",
    "EmbeddingError",
    "LocalEmbedding",
    "LocalFastEmbedEmbedder",
    "OpenAiConfig",
    "OpenAiDumpConfig",
    "OpenAiEmbedder",
    "OpenAiEmbedding",
    "OpenAiHttp",
]
