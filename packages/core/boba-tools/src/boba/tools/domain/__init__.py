"""Boba tool domain: contract layer для tools framework и LLM-adapter'ов.

Содержит:
- Identity: ToolName (оно же wire-имя), ToolSourceId, ToolId.
- Tool ABC + ToolCall, ToolContext.
- ToolResult sealed family (TextResult / JsonResult / ErrorResult).
- доменные ошибки: ToolExecutionError, InvalidToolArgumentError, etc.

Application-фреймворк (registry, ToolRegistry, ToolCatalog, ToolExecutor,
plugin_loader) — в `boba-tools`. Адаптеры под конкретные LLM-API живут
в `boba-adapter-*` и работают с `ToolResult` через `match`-разбор по
discriminator `kind`.
"""

from __future__ import annotations

from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
    ToolExecutionError,
    ToolOutputTooLargeError,
)
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    to_tool_id,
)
from boba.tools.domain.result import (
    ChartResult,
    ErrorResult,
    JsonResult,
    PgCopyTextResult,
    TableResult,
    TextResult,
    ToolResult,
)
from boba.tools.domain.tool import (
    Tool,
    ToolCall,
    ToolContext,
    ToolSchema,
)

__all__ = [
    "ChartResult",
    "ErrorResult",
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "JsonResult",
    "PgCopyTextResult",
    "TableResult",
    "TextResult",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolExecutionError",
    "ToolId",
    "ToolName",
    "ToolOutputTooLargeError",
    "ToolResult",
    "ToolSchema",
    "ToolSourceId",
    "to_tool_id",
]
