"""boba.coercion: пайплайн обработки значения для FieldSpec.coercer.

- base.py            — Coercer ABC, MISSING, Pass, ChainCoercer, ValueCoercer.
- preconditions.py   — NotNull, Required, Default, Nullable (наличие значения).
- types.py           — Parse* (приведение типа) и Is* (строгий type-guard).
- constraints.py     — OneOf / Min*/Max* / NonEmpty (ограничения).
- invariants.py      — Ordered / MutuallyExclusive / RequiresTogether
                       / RequiredWhen (object-level).
"""

from boba.schema.coercion.base import (
    MISSING,
    ChainCoercer,
    Coercer,
    Pass,
    SchemaContributor,
    ValueCoercer,
)
from boba.schema.coercion.constraints import (
    MaxLength,
    MaxValue,
    MinLength,
    MinValue,
    NonEmpty,
    OneOf,
)
from boba.schema.coercion.invariants import (
    MutuallyExclusive,
    Ordered,
    RequiredWhen,
    RequiresTogether,
)
from boba.schema.coercion.preconditions import (
    Default,
    NotNull,
    Nullable,
    Required,
)
from boba.schema.coercion.types import (
    IsBool,
    IsInt,
    IsNumber,
    IsString,
    ParseBool,
    ParseCsvList,
    ParseFloat,
    ParseInt,
    ParseString,
)

__all__ = [
    "MISSING",
    "ChainCoercer",
    "Coercer",
    "Default",
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
    "NotNull",
    "Nullable",
    "OneOf",
    "Ordered",
    "ParseBool",
    "ParseCsvList",
    "ParseFloat",
    "ParseInt",
    "ParseString",
    "Pass",
    "Required",
    "RequiredWhen",
    "RequiresTogether",
    "SchemaContributor",
    "ValueCoercer",
]
