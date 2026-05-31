"""Идентификаторы tool-домена.

Имя tool'а живёт в **двух пространствах**:

- `ToolName` — имя tool'а внутри своего `ToolSource`.
- `ToolId` — wire-формат, по которому tool вызывается LLM-ом. Равен самому
  `ToolName`: источник в имя не входит, LLM видит ровно `<name>` и по нему же
  маршрутизируется вызов. Глобальная уникальность `ToolName` гарантируется
  реестром (`ToolNameCollisionError` при дубле между source'ами).

Wire-имя ограничено `^[A-Za-z0-9_-]{1,64}$` — это пересечение OpenAI
function-name spec и того, что без сюрпризов проходит через
LiteLLM/Ollama.
"""

from __future__ import annotations

import re
from typing import NewType

__all__ = [
    "ToolId",
    "ToolName",
    "ToolSourceId",
    "sanitize_source_id",
    "to_tool_id",
]

_SEPARATOR = "__"
_MAX_TOOL_ID_LENGTH = 64
_COMPONENT_CHARS = r"A-Za-z0-9_-"
_TOOL_NAME_RE = re.compile(rf"^[A-Za-z0-9][{_COMPONENT_CHARS}]*$")
_NON_COMPONENT_RE = re.compile(rf"[^{_COMPONENT_CHARS}]")


ToolName = NewType("ToolName", str)
"""Имя инструмента внутри своего source'а; оно же — wire-имя."""


ToolSourceId = NewType("ToolSourceId", str)
"""Идентификатор источника инструментов (`plugin_html`, `mcp_github`, ...)."""


ToolId = NewType("ToolId", str)
"""Wire-формат: совпадает с `ToolName`. То, что видит и зовёт LLM."""


def to_tool_id(name: ToolName) -> ToolId:
    """Привести `ToolName` к wire-формату `ToolId` (валидация charset/длины)."""
    if not name:
        msg = "tool name must be non-empty"
        raise ValueError(msg)
    if not _TOOL_NAME_RE.match(name):
        msg = (
            f"invalid tool name {name!r}: must match [A-Za-z0-9][A-Za-z0-9_-]* "
            f"(letters/digits/_/-, leading char alphanumeric)"
        )
        raise ValueError(msg)
    if len(name) > _MAX_TOOL_ID_LENGTH:
        msg = (
            f"tool name {name!r} is {len(name)} chars, "
            f"max {_MAX_TOOL_ID_LENGTH} (OpenAI function-name limit)"
        )
        raise ValueError(msg)
    return ToolId(name)


def sanitize_source_id(origin: str) -> ToolSourceId:
    """
    Привести произвольный `origin` (имя модуля/плагина) к валидному ToolSourceId.
    """
    sanitized = _NON_COMPONENT_RE.sub("_", origin)
    while _SEPARATOR in sanitized:
        sanitized = sanitized.replace(_SEPARATOR, "_")
    sanitized = sanitized.strip("_-")
    return ToolSourceId(sanitized or "plugin")
