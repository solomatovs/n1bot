"""OpenAI-совместимый LLM-адаптер (LiteLLM/Ollama тоже).

- OpenAITerminal — StreamSource[LLMContext, LLMEvent] поверх openai-SDK;
- build_openai_client — фабрика SDK-клиента из LLMConfig;
- WireTraceChatCompletionObserver / TranscriptChatCompletionObserver /
  MetricsChatCompletionObserver — биндинги доменного LLMRequestObserver
  под OpenAI Chat Completions API;
- DuplicateToolCallIndexReindexer — workaround для коллизии index у
  параллельных tool_calls.

Отдельный pip-пакет; core boba от него не зависит.
"""

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
from boba.domain.llm.observer import RequestOutcome

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
