"""Boba tool domain: contract layer для tools framework и LLM-adapter'ов.

Содержит:
- Identity: ToolName (локальное), ToolSourceId, ToolId (qualified wire).
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
    ToolIdCollisionError,
    ToolOutputTooLargeError,
    ToolSourceCollisionError,
)
from boba.tools.domain.events import (
    BaseToolEvent,
    ToolEvent,
    ToolProgressReported,
    ToolSeverity,
    ToolStreamCompleted,
)
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    compose_tool_id,
    parse_tool_id,
)
from boba.tools.domain.result import (
    ErrorResult,
    JsonResult,
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
    "BaseToolEvent",
    "ErrorResult",
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "JsonResult",
    "TextResult",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolEvent",
    "ToolExecutionError",
    "ToolId",
    "ToolIdCollisionError",
    "ToolName",
    "ToolOutputTooLargeError",
    "ToolProgressReported",
    "ToolResult",
    "ToolSchema",
    "ToolSeverity",
    "ToolSourceCollisionError",
    "ToolSourceId",
    "ToolStreamCompleted",
    "compose_tool_id",
    "parse_tool_id",
]
