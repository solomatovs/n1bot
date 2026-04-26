"""Tool framework: схема параметров, конвертеры, базовый Tool, реестр.

Публичный API пакета — все его символы реэкспортируются здесь и доступны
как ``from boba.domain.core.tools import X``. Внутренняя структура файлов
— деталь реализации.
"""

from boba.domain.core.config import FieldSpec, ObjectSchema, ObjectWireSchema
from boba.domain.core.schema import ParamWireSchema, SchemaContributor
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
    ToolDefinition,
    ToolId,
    ToolInputSchema,
    ToolSourceId,
)
from boba.domain.core.tools.tool import Tool, ToolCall, ToolContext, ToolResult
from boba.domain.core.tools.validators import (
    IsBool,
    IsInt,
    IsNumber,
    IsString,
    MutuallyExclusive,
    Ordered,
    RequiresTogether,
    SchemaArgsValidator,
)
from boba.domain.core.validators import (
    MISSING,
    ChainConverter,
    Default,
    MaxLength,
    MaxValue,
    MinLength,
    MinValue,
    NonEmpty,
    Nullable,
    OneOf,
    ParseBool,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
    Pass,
    Required,
    ValueConverter,
)

__all__ = [
    "MISSING",
    "ChainConverter",
    "Default",
    "FieldSpec",
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
    "Nullable",
    "ObjectSchema",
    "ObjectWireSchema",
    "OneOf",
    "Ordered",
    "ParamWireSchema",
    "ParseBool",
    "ParseCsvList",
    "ParseFloat",
    "ParseInt",
    "ParseString",
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
    "ValueConverter",
]
