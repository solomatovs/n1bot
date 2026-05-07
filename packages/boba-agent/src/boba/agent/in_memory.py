"""In-memory реализация MessageService — дефолт AgentBuilder'а."""

from __future__ import annotations

from collections.abc import Iterator

from boba.agent.messages import MessageService
from boba.llm.models import LLMMessage


class InMemoryMessageService(MessageService):
    """In-memory реализация MessageService."""

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
