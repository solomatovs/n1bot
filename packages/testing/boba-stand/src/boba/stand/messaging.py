"""Тестовая шина сообщений с тем же каталогом моделей, что и AID."""

from __future__ import annotations

from boba.chat.messages import ChatMessages
from boba.identity.context import Scope
from boba.messaging import LockToken, MemoryMessageBus


def memory_message_bus(origin: str) -> MemoryMessageBus:
    """Создаёт локальную шину для тестов, не проверяющих право держателя хода."""
    codec = ChatMessages().codec

    def allow_test_event(scope: Scope, token: LockToken) -> None:
        del scope, token

    return MemoryMessageBus(origin, codec, allow_test_event)
