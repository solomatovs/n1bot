"""Идентификаторы tool-домена.

Имя tool'а живёт в **двух пространствах**:

- `ToolName` — локальное имя в пределах своего `ToolSource` (например,
  `outline` внутри `plugin.html`). Уникальность гарантируется самим
  source'ом — глобальной коллизии нет.
- `ToolId` — wire-формат, по которому tool вызывается LLM-ом:
  `<source_id>/<name>`. Единственная точка композиции/парсинга —
  `compose_tool_id`/`parse_tool_id`.
"""

from __future__ import annotations

from typing import NewType

__all__ = [
    "ToolId",
    "ToolName",
    "ToolSourceId",
    "compose_tool_id",
    "parse_tool_id",
]

_SEPARATOR = "/"


ToolName = NewType("ToolName", str)
"""Локальное имя инструмента внутри своего source'а."""


ToolSourceId = NewType("ToolSourceId", str)
"""Идентификатор источника инструментов (`plugin.html`, `mcp.github`, ...)."""


ToolId = NewType("ToolId", str)
"""Wire-формат: `<source_id>/<tool_name>`. Композируется из пары."""


def compose_tool_id(source_id: ToolSourceId, name: ToolName) -> ToolId:
    """Скомпоновать qualified `<source>/<name>` ToolId."""
    return ToolId(f"{source_id}{_SEPARATOR}{name}")


def parse_tool_id(tool_id: ToolId) -> tuple[ToolSourceId, ToolName]:
    """`plugin.html/outline` → `(ToolSourceId('plugin.html'), ToolName('outline'))`."""
    source_part, sep, name_part = tool_id.partition(_SEPARATOR)
    if not sep or not name_part:
        msg = (
            f"invalid qualified tool id {tool_id!r}: expected '<source>/<name>'"
        )
        raise ValueError(msg)
    return ToolSourceId(source_part), ToolName(name_part)
