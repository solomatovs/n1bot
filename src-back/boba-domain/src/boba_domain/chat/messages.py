"""LLM Messages — доменная модель сообщений."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class LLMRole(str, Enum):
    """Роли сообщений."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class LLMMessage:
    """Одно сообщение в диалоге.

    Чистый domain-тип. Конвертация в API-формат — в адаптере.
    """

    role: LLMRole
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
