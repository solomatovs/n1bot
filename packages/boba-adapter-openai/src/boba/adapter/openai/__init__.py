"""OpenAI-совместимый LLM-адаптер."""

from boba.adapter.openai.config import LLMTransportSection, create_llm_source
from boba.adapter.openai.observer import (
    MetricsChatCompletionObserver,
    MultiKeyReasoningExtractor,
    TranscriptChatCompletionObserver,
    WireTraceChatCompletionObserver,
)
from boba.adapter.openai.terminal import OpenAITerminal, build_openai_client
from boba.adapter.openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)
from boba.llm.observer import RequestOutcome

__all__ = [
    "DuplicateToolCallIndexReindexer",
    "LLMTransportSection",
    "MetricsChatCompletionObserver",
    "MultiKeyReasoningExtractor",
    "OpenAITerminal",
    "RequestOutcome",
    "TranscriptChatCompletionObserver",
    "WireTraceChatCompletionObserver",
    "build_openai_client",
    "create_llm_source",
]
