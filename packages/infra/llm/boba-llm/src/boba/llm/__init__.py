"""Единая точка работы с LLM-провайдерами: транспорт, чат, эмбеддинги.

Стандарт провайдеров: каждая способность — порт, union-конфиг с
дискриминатором kind (boba.chat) и фабрика здесь. Поведение HTTP общее
для всех провайдеров: секция [http] и компоненты модуля http. Модули с
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
from boba.llm.http import ChatExchange, LlmHttp

__all__ = [
    "ChatExchange",
    "EmbedderFactory",
    "EmbeddingConfig",
    "EmbeddingError",
    "LlmHttp",
    "LocalEmbedding",
    "LocalFastEmbedEmbedder",
    "OpenAiEmbedder",
    "OpenAiEmbedding",
]
