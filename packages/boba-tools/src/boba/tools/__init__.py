"""Tools-домен: Tool ABC, диспетчер вызовов, валидация args, wire-схемы."""

from boba.tools.args import ToolArgsBuilder
from boba.tools.descriptions import ParamOverlay, param_desc, params_field
from boba.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
    ToolExecutionError,
    ToolIdCollisionError,
    ToolOutputTooLargeError,
)
from boba.tools.ids import ToolId, ToolSourceId
from boba.tools.plugin_loader import (
    ENTRY_POINTS_GROUP,
    ExtensionContext,
    ToolPluginError,
    ToolPluginLoader,
    ToolPluginLoadError,
    ToolPluginRegisterError,
)
from boba.tools.registry import (
    StaticToolSource,
    ToolCatalog,
    ToolFactory,
    ToolSource,
    ToolsService,
    ToolStore,
)
from boba.tools.tool import (
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
)
from boba.tools.wire import ToolWireSchemaBuilder

__all__ = [
    "ENTRY_POINTS_GROUP",
    "ExtensionContext",
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "ParamOverlay",
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
    "ToolOutputTooLargeError",
    "ToolPluginError",
    "ToolPluginLoadError",
    "ToolPluginLoader",
    "ToolPluginRegisterError",
    "ToolResult",
    "ToolSource",
    "ToolSourceId",
    "ToolStore",
    "ToolWireSchemaBuilder",
    "ToolsService",
    "param_desc",
    "params_field",
]
