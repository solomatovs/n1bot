"""Провод хука `PreToolUse` эндпоинта chainlit: тело события и тело ответа.

Агент хуки не исполняет; модели нужны эндпоинту хука chainlit и уходят
вместе с ним.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from boba.agent.records import PermissionMode

__all__ = [
    "HookDecision",
    "HookEventName",
    "HookReplyBody",
    "HookReplyWire",
    "HookSpecificOutput",
    "HookWire",
    "McpServerInfo",
    "PreToolUseEvent",
]


class HookEventName(StrEnum):
    """События хука."""

    PRE_TOOL_USE = "PreToolUse"


class HookDecision(StrEnum):
    """Решение обработчика о вызове."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class HookWire(BaseModel):
    """Основа моделей события: ключи в snake_case, как в форме хуков."""

    model_config = ConfigDict(frozen=True)


class McpServerInfo(HookWire):
    """Расширение события: сервер MCP инструмента и его возможности."""

    name: str
    features: Mapping[str, Mapping[str, object]] = Field(default_factory=dict)


class PreToolUseEvent(HookWire):
    """Тело `POST` обработчику. Поля формы хуков: `hook_event_name`,
    `session_id`, `cwd`, `permission_mode`, `tool_name`, `tool_input`,
    `tool_use_id`. Расширение: `tool_schema` — входная JSON-схема
    инструмента; `mcp_server` — сервер MCP и его возможности."""

    hook_event_name: Literal[HookEventName.PRE_TOOL_USE] = HookEventName.PRE_TOOL_USE
    session_id: str
    cwd: str
    permission_mode: PermissionMode
    tool_name: str
    tool_input: Mapping[str, Any]
    tool_use_id: str
    tool_schema: Mapping[str, Any]
    mcp_server: McpServerInfo | None = None


class HookReplyWire(BaseModel):
    """Основа моделей ответа: ключи в camelCase, чужие поля пропускаются."""

    model_config = ConfigDict(
        frozen=True, alias_generator=to_camel, populate_by_name=True, extra="ignore"
    )


class HookSpecificOutput(HookReplyWire):
    """`hookSpecificOutput` ответа: решение, причина, замена аргументов."""

    hook_event_name: str = ""
    permission_decision: HookDecision | None = None
    permission_decision_reason: str = ""
    updated_input: Mapping[str, Any] | None = None


class HookReplyBody(HookReplyWire):
    """JSON-объект ответа обработчика; без `hookSpecificOutput` — решения нет."""

    hook_specific_output: HookSpecificOutput | None = None
