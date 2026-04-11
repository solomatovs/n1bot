"""LLM Messages — типизированная модель для отправки в API."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class LLMRole(str, Enum):
    """Роли в OpenAI-совместимом API."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class LLMMessage:
    """Одно сообщение для OpenAI-совместимого API.

    Поддерживает стандартные сообщения (role + content),
    assistant-сообщения с tool_calls и tool-результаты.
    """
    role: LLMRole
    content: str = ""
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role.value}
        if self.content:
            d["content"] = self.content
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d
