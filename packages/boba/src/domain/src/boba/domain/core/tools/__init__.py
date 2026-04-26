"""Tool framework: схема параметров, конвертеры, базовый Tool, реестр.

Публичный API пакета — все его символы реэкспортируются здесь и доступны
как ``from boba.domain.core.tools import X``. Внутренняя структура файлов
— деталь реализации.
"""

from boba.domain.core.declaration import FieldSpec, ObjectSchema, validate_object
from boba.domain.core.schema import (
    ObjectWireSchema,
    ParamWireSchema,
    SchemaContributor,
)
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
    ToolId,
    ToolSourceId,
)
from boba.domain.core.tools.tool import (
    SchemaArgsValidator,
    Tool,
    ToolCall,
    ToolContext,
    ToolResult,
)
from boba.domain.core.validators import (
    MISSING,
    ChainConverter,
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
    Nullable,
    OneOf,
    Ordered,
    ParseBool,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
    Pass,
    Required,
    RequiresTogether,
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
    "ToolExecutionError",
    "ToolFactory",
    "ToolId",
    "ToolIdCollisionError",
    "ToolOutputTooLargeError",
    "ToolResult",
    "ToolSource",
    "ToolSourceId",
    "ToolStore",
    "ToolsService",
    "ValueConverter",
    "validate_object",
]
