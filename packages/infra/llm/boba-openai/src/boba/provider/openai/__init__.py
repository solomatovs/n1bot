"""OpenAI-совместимый LLM-адаптер."""

from boba.provider.openai.compat_embedder import OpenAICompatEmbedder
from boba.provider.openai.config import use_openai
from boba.provider.openai.dto import OpenAIConfig
from boba.provider.openai.embedder import OpenAIEmbedder
from boba.provider.openai.observer import (
    CurlTraceChatCompletionObserver,
    HttpTraceChatCompletionObserver,
    MetricsChatCompletionObserver,
    MultiKeyReasoningExtractor,
)
from boba.provider.openai.terminal import OpenAITerminal, build_openai_client
from boba.provider.openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)

__all__ = [
    "CurlTraceChatCompletionObserver",
    "DuplicateToolCallIndexReindexer",
    "HttpTraceChatCompletionObserver",
    "MetricsChatCompletionObserver",
    "MultiKeyReasoningExtractor",
    "OpenAICompatEmbedder",
    "OpenAIConfig",
    "OpenAIEmbedder",
    "OpenAITerminal",
    "build_openai_client",
    "use_openai",
]
