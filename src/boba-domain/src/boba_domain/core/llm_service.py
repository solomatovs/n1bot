"""Протокол LLM completion service.

Абстракция над LLM-провайдером. Application-слой работает
только с CompletionDelta, не знает про OpenAI SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Protocol, Sequence

from boba_domain.chat.messages import LLMMessage


@dataclass(frozen=True)
class CompletionDelta:
    """Один чанк стриминг-ответа LLM, выраженный в доменных терминах."""
    content: str | None = None
    reasoning_content: str | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments: str | None = None


class LLMCompletionService(Protocol):
    """Стриминг LLM completion."""

    def stream_completion(
        self,
        messages: Sequence[LLMMessage],
        tool_definitions: Sequence[Dict[str, Any]],
        model: str,
    ) -> Iterator[CompletionDelta]:
        """Yield CompletionDelta из LLM."""
        ...
