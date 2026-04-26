"""Tool framework: схема параметров, валидаторы, базовый Tool, реестр.

Публичный API пакета — все его символы реэкспортируются здесь и доступны
как ``from boba.domain.core.tools import X``. Внутренняя структура файлов
— деталь реализации.
"""

from boba.domain.core.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
    ToolExecutionError,
    ToolIdCollisionError,
    ToolOutputTooLargeError,
)
from boba.domain.core.tools.registry import (
    ToolCatalog,
    ToolFactory,
    ToolSource,
    ToolsService,
    ToolStore,
)
from boba.domain.core.tools.schema import (
    ParamSchema,
    ParamWireSchema,
    SchemaContributor,
    ToolDefinition,
    ToolId,
    ToolInputSchema,
    ToolSourceId,
)
from boba.domain.core.tools.tool import Tool, ToolCall, ToolContext, ToolResult
from boba.domain.core.tools.validators import (
    MISSING,
    ChainValidator,
    Default,
    IsBool,
    IsInt,
    IsNumber,
    IsString,
    MaxLength,
    MaxValue,
    MinLength,
    MinValue,
    MutuallyExclusive,
    NonEmpty,
    OneOf,
    Ordered,
    ParamValidationError,
    Pass,
    Required,
    RequiresTogether,
    SchemaArgsValidator,
    ValueValidator,
)

__all__ = [
    "MISSING",
    "ChainValidator",
    "Default",
    "InvalidSchemaInvariantError",
    "InvalidToolArgumentError",
    "IsBool",
    "IsInt",
    "IsNumber",
    "IsString",
    "MaxLength",
    "MaxValue",
    "MinLength",
    "MinValue",
    "MutuallyExclusive",
    "NonEmpty",
    "OneOf",
    "Ordered",
    "ParamSchema",
    "ParamValidationError",
    "ParamWireSchema",
    "Pass",
    "Required",
    "RequiresTogether",
    "SchemaArgsValidator",
    "SchemaContributor",
    "Tool",
    "ToolCall",
    "ToolCatalog",
    "ToolContext",
    "ToolDefinition",
    "ToolExecutionError",
    "ToolFactory",
    "ToolId",
    "ToolIdCollisionError",
    "ToolInputSchema",
    "ToolOutputTooLargeError",
    "ToolResult",
    "ToolSource",
    "ToolSourceId",
    "ToolStore",
    "ToolsService",
    "ValueValidator",
]
