from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict

from boba.agent._pydantic_compat import ToolResultField


class ToolCallResult(BaseModel):
    """Результат успешного выполнения tool — доменный ToolResult."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ToolResultField


class ToolCallFailure(BaseModel):
    """Tool бросил ToolExecutionError или невалидный JSON в args."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
