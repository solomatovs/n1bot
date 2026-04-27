"""Реализации MessageService — журнал сообщений диалога."""

from boba.adapter.messages.in_memory import InMemoryMessageService
from boba.adapter.messages.jsonl import JsonLinesMessageService

__all__ = [
    "InMemoryMessageService",
    "JsonLinesMessageService",
]
