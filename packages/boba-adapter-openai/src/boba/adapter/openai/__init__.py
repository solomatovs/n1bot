"""OpenAI-совместимый LLM-адаптер (LiteLLM/Ollama тоже).

- OpenAITerminal — StreamSource[LLMContext, LLMEvent] поверх openai-SDK;
- build_openai_client — фабрика SDK-клиента из LLMConfig;
- RawLLMObserver — наблюдатели сырых kwargs/chunks (file/content/metrics);
- DuplicateToolCallIndexReindexer — workaround для коллизии index у
  параллельных tool_calls.

Отдельный pip-пакет; core boba от него не зависит.
"""

from boba.adapter.openai.config import LLMTransportSection, create_llm_source
from boba.adapter.openai.raw_observer import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
    MetricsRawLLMObserver,
    MultiKeyReasoningExtractor,
    RawLLMObserver,
    RequestOutcome,
)
from boba.adapter.openai.terminal import OpenAITerminal, build_openai_client
from boba.adapter.openai.tool_call_reindexer import (
    DuplicateToolCallIndexReindexer,
)

__all__ = [
    "CompositeRawLLMObserver",
    "DuplicateToolCallIndexReindexer",
    "FileContentObserver",
    "FileRawLLMObserver",
    "LLMTransportSection",
    "MetricsRawLLMObserver",
    "MultiKeyReasoningExtractor",
    "OpenAITerminal",
    "RawLLMObserver",
    "RequestOutcome",
    "build_openai_client",
    "create_llm_source",
]
