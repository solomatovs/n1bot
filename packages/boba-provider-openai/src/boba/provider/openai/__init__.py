"""OpenAI-совместимый LLM-адаптер."""

from boba.llm.observer import RequestOutcome
from boba.provider.openai.config import create_llm_source
from boba.provider.openai.dto import OpenAIConfig
from boba.provider.openai.observer import (
    CurlTraceChatCompletionObserver,
    MetricsChatCompletionObserver,
    MultiKeyReasoningExtractor,
)
from boba.provider.openai.terminal import OpenAITerminal, build_openai_client
from boba.provider.openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)
from boba.provider.openai.visitor import OpenAIChatVisitor

__all__ = [
    "CurlTraceChatCompletionObserver",
    "DuplicateToolCallIndexReindexer",
    "MetricsChatCompletionObserver",
    "MultiKeyReasoningExtractor",
    "OpenAIChatVisitor",
    "OpenAIConfig",
    "OpenAITerminal",
    "RequestOutcome",
    "build_openai_client",
    "create_llm_source",
]
