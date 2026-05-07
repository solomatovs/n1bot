"""Boba tool domain: contract layer для tools framework и LLM-adapter'ов.

Содержит:
- Sealed identity: ToolId, ToolSourceId.
- Tool ABC + ToolCall, ToolContext.
- ToolResult sealed family + ToolResultVisitor (double-dispatch).
- args/wire builders: типизация tool-arguments + JSON-Schema export.
- doменные ошибки: ToolExecutionError, InvalidToolArgumentError, etc.

Application-фреймворк (registry, ToolsService, plugin_loader) — в `boba-tools`.
LLM-adapter'ы (`boba-adapter-*`) реализуют `ToolResultVisitor` под свой
target-формат (str, multi-part, structured-output).
"""

from __future__ import annotations

from boba.tools.domain.args import ToolArgsBuilder
from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
    ToolExecutionError,
    ToolIdCollisionError,
    ToolOutputTooLargeError,
)
from boba.tools.domain.ids import ToolId, ToolSourceId
from boba.tools.domain.result import (
    JsonResult,
    TextResult,
    ToolResult,
    ToolResultVisitor,
)
from boba.tools.domain.tool import Tool, ToolCall, ToolContext
from boba.tools.domain.wire import ToolWireSchemaBuilder

__all__ = [
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "JsonResult",
    "TextResult",
    "Tool",
    "ToolArgsBuilder",
    "ToolCall",
    "ToolContext",
    "ToolExecutionError",
    "ToolId",
    "ToolIdCollisionError",
    "ToolOutputTooLargeError",
    "ToolResult",
    "ToolResultVisitor",
    "ToolSourceId",
    "ToolWireSchemaBuilder",
]
