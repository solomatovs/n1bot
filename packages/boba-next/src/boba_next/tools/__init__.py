"""Tools-домен: Tool ABC, диспетчер вызовов, валидация args, wire-схемы."""

from boba_next.tools.args import ToolArgsBuilder
from boba_next.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
    ToolExecutionError,
    ToolIdCollisionError,
    ToolOutputTooLargeError,
)
from boba_next.tools.ids import ToolId, ToolSourceId
from boba_next.tools.registry import (
    StaticToolSource,
    ToolCatalog,
    ToolFactory,
    ToolSource,
    ToolsService,
    ToolStore,
)
from boba_next.tools.specs import ToolNameIn, ToolSourceIn
from boba_next.tools.tool import (
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
)
from boba_next.tools.wire import ToolWireSchemaBuilder

__all__ = [
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "StaticToolSource",
    "Tool",
    "ToolArgsBuilder",
    "ToolCall",
    "ToolCatalog",
    "ToolContext",
    "ToolExecutionError",
    "ToolFactory",
    "ToolId",
    "ToolIdCollisionError",
    "ToolNameIn",
    "ToolOutputTooLargeError",
    "ToolResult",
    "ToolSource",
    "ToolSourceId",
    "ToolSourceIn",
    "ToolStore",
    "ToolWireSchemaBuilder",
    "ToolsService",
]
