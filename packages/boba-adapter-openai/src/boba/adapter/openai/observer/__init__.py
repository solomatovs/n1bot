"""Наблюдатели OpenAI Chat Completions API.

Биндят доменный LLMRequestObserver[TRequest, TChunk] под конкретные
типы OpenAI SDK (dict[str, Any] и ChatCompletionChunk). Используются
для отладки, сбора датасетов, метрик — на wire-слое, до доменной
конверсии в LLMEvent.
"""

from boba.adapter.openai.observer.metrics import MetricsChatCompletionObserver
from boba.adapter.openai.observer.reasoning import MultiKeyReasoningExtractor
from boba.adapter.openai.observer.transcript import (
    TranscriptChatCompletionObserver,
)
from boba.adapter.openai.observer.wire_trace import (
    WireTraceChatCompletionObserver,
)

__all__ = [
    "MetricsChatCompletionObserver",
    "MultiKeyReasoningExtractor",
    "TranscriptChatCompletionObserver",
    "WireTraceChatCompletionObserver",
]
