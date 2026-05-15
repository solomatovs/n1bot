"""Boba tool domain: contract layer для tools framework и LLM-adapter'ов.

Содержит:
- Identity: ToolName (локальное), ToolSourceId, ToolId (qualified wire).
- Tool ABC + ToolCall, ToolContext, JsonSchemaOverlay-protocol.
- ToolResult sealed family + ToolResultVisitor (double-dispatch).
- доменные ошибки: ToolExecutionError, InvalidToolArgumentError, etc.

Application-фреймворк (registry, ToolExecutor, plugin_loader) — в `boba-tools`.
LLM-adapter'ы (`boba-adapter-*`) реализуют `ToolResultVisitor` под свой
target-формат (str, multi-part, structured-output).
"""

from __future__ import annotations

from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
    ToolExecutionError,
    ToolIdCollisionError,
    ToolOutputTooLargeError,
    ToolSourceCollisionError,
)
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    compose_tool_id,
    parse_tool_id,
)
from boba.tools.domain.llm_schema import clean_llm_json_schema
from boba.tools.domain.result import (
    DefaultTextVisitor,
    ErrorResult,
    JsonResult,
    TextResult,
    ToolResult,
    ToolResultVisitor,
)
from boba.tools.domain.tool import (
    JsonSchemaOverlay,
    Tool,
    ToolCall,
    ToolContext,
)

__all__ = [
    "DefaultTextVisitor",
    "ErrorResult",
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "JsonResult",
    "JsonSchemaOverlay",
    "TextResult",
    "Tool",
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
    "clean_llm_json_schema",
    "compose_tool_id",
    "parse_tool_id",
]
