"""Boba tool domain: contract layer для tools framework и LLM-adapter'ов.

Содержит:
- Identity: ToolName (локальное), ToolSourceId, ToolId (qualified wire).
- Tool ABC + ToolCall, ToolContext.
- ToolResult sealed family + ToolResultVisitor (double-dispatch).
- args/wire builders: типизация tool-arguments + JSON-Schema export.
- доменные ошибки: ToolExecutionError, InvalidToolArgumentError, etc.

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
    ToolSourceCollisionError,
)
from boba.tools.domain.ids import ToolId, ToolName, ToolSourceId
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
    "ToolName",
    "ToolOutputTooLargeError",
    "ToolResult",
    "ToolResultVisitor",
    "ToolSourceCollisionError",
    "ToolSourceId",
    "ToolWireSchemaBuilder",
]
