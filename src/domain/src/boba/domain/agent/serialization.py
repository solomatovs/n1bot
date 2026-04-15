"""Типы сериализации AgentEvent."""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent
from boba.domain.core.patterns import Converter, Serializer

# Типы конвертеров для AgentEvent
EventEncoder = Converter[AgentEvent, tuple[str, dict]]
EventDecoder = Converter[tuple[str, dict], AgentEvent]

# Сериализатор = композиция encoder + decoder
EventSerializer = Serializer[AgentEvent, tuple[str, dict]]
