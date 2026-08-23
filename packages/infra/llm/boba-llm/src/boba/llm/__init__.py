"""Единая точка работы с LLM-провайдерами: транспорт, чат, эмбеддинги.

Стандарт провайдеров: каждая способность — порт, union-конфиг с
дискриминатором provider и фабрика (см. boba.llm.provider). Модули с
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
from boba.llm.openai import OpenAiConfig, OpenAiDumpConfig, OpenAiHttp
from boba.llm.provider import (
    ChatBackendConfig,
    ChatDelta,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatSampling,
    ChatTurn,
    LocalChatConfig,
    OpenAiChatConfig,
    ToolCallRequest,
    ToolSpec,
)

__all__ = [
    "ChatBackendConfig",
    "ChatDelta",
    "ChatEvent",
    "ChatProvider",
    "ChatProviderError",
    "ChatReply",
    "ChatRequest",
    "ChatRole",
    "ChatSampling",
    "ChatTurn",
    "EmbedderFactory",
    "EmbeddingConfig",
    "EmbeddingError",
    "LocalChatConfig",
    "LocalEmbedding",
    "LocalFastEmbedEmbedder",
    "OpenAiChatConfig",
    "OpenAiConfig",
    "OpenAiDumpConfig",
    "OpenAiEmbedder",
    "OpenAiEmbedding",
    "OpenAiHttp",
    "ToolCallRequest",
    "ToolSpec",
]
