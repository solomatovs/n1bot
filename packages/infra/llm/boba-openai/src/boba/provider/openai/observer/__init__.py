"""Наблюдатели OpenAI Chat Completions API."""

from boba.provider.openai.observer.curl_trace import (
    CurlTraceChatCompletionObserver,
)
from boba.provider.openai.observer.http_trace import (
    HttpTraceChatCompletionObserver,
)

__all__ = [
    "CurlTraceChatCompletionObserver",
    "HttpTraceChatCompletionObserver",
]
