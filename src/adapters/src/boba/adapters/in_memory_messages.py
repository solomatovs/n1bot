"""Простейшая реализация MessageService — список в памяти."""

from __future__ import annotations

from typing import Iterator
from boba.domain.llm.llm import LLMMessage
from boba.domain.core.messages import MessageService


class InMemoryMessageService(MessageService):

    def __init__(self) -> None:
        self._messages: list[LLMMessage] = []

    def add(self, message: LLMMessage) -> None:
        self._messages.append(message)

    def message_iter(self) -> Iterator[LLMMessage]:
        return iter(self._messages)

    def last(self) -> LLMMessage | None:
        return self._messages[-1] if self._messages else None

    def clear(self) -> None:
        self._messages.clear()
