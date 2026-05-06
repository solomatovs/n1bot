"""OpenAI-совместимый LLM-адаптер."""

from boba.adapter.openai.config import OpenAIAdapterSection, create_llm_source
from boba.adapter.openai.observer import (
    CurlTraceChatCompletionObserver,
    MetricsChatCompletionObserver,
    MultiKeyReasoningExtractor,
)
from boba.adapter.openai.terminal import OpenAITerminal, build_openai_client
from boba.adapter.openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)
from boba.adapter.openai.visitor import OpenAIChatVisitor
from boba.llm.observer import RequestOutcome

__all__ = [
    "CurlTraceChatCompletionObserver",
    "DuplicateToolCallIndexReindexer",
    "MetricsChatCompletionObserver",
    "MultiKeyReasoningExtractor",
    "OpenAIAdapterSection",
    "OpenAIChatVisitor",
    "OpenAITerminal",
    "RequestOutcome",
    "build_openai_client",
    "create_llm_source",
]
