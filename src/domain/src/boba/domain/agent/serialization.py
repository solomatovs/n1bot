"""Типы сериализации AgentEvent."""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent
from boba.domain.core.patterns import Converter, Serializer

# Типы конвертеров для AgentEvent
EventEncoder = Converter[AgentEvent, str]
EventDecoder = Converter[str, AgentEvent]

# Сериализатор = композиция encoder + decoder
EventSerializer = Serializer[AgentEvent, str]
