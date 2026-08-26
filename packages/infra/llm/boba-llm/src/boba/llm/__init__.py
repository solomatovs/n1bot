"""Единая точка работы с LLM-провайдерами: транспорт, чат, эмбеддинги.

Стандарт провайдеров: каждая способность — порт, union-конфиг с
дискриминатором provider (boba.chat) и фабрика здесь. Модули с
langchain (chat, bridge) и локальным рантаймом (local) импортируются
напрямую: их зависимостей нет в песочных payload-окружениях.

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
from boba.llm.openai import OpenAiHttp

__all__ = [
    "EmbedderFactory",
    "EmbeddingConfig",
    "EmbeddingError",
    "LocalEmbedding",
    "LocalFastEmbedEmbedder",
    "OpenAiEmbedder",
    "OpenAiEmbedding",
    "OpenAiHttp",
]
