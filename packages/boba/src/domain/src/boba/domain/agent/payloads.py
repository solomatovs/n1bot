"""Value-объекты для payload'ов агентских событий."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class ToolCallResult:
    """Результат успешного выполнения tool."""

    content: str


@dataclass(frozen=True)
class ToolCallFailure:
    """Tool бросил ToolExecutionError или невалидный JSON в args."""

    error_kind: str
    message: str

@dataclass(frozen=True)
class LLMCritique:
    """Общая критика к LLM (role="user"), не привязана к tool_call."""

    content: str


@dataclass(frozen=True)
class ToolCallRejection:
    """Подавление tool_call'а: ответ в слот вызова (role="tool")."""

    tool_call_id: str
    content: str


LLMFeedback: TypeAlias = LLMCritique | ToolCallRejection
