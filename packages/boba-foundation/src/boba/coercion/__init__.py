"""boba.coercion: пайплайн обработки значения для FieldSpec.coercer.

- base.py            — Coercer ABC, MISSING, Pass, ChainCoercer, ValueCoercer.
- preconditions.py   — NotNull, Default, Nullable (наличие значения).
- types.py           — Parse* (приведение типа) и Is* (строгий type-guard).
- constraints.py     — OneOf / Min*/Max* / NonEmpty (ограничения).
- invariants.py      — MutuallyExclusive / RequiresTogether / Ordered (object-level).
"""

from boba.coercion.base import (
    MISSING,
    ChainCoercer,
    Coercer,
    Pass,
    SchemaContributor,
    ValueCoercer,
)
from boba.coercion.constraints import (
    MaxLength,
    MaxValue,
    MinLength,
    MinValue,
    NonEmpty,
    OneOf,
)
from boba.coercion.invariants import (
    MutuallyExclusive,
    Ordered,
    RequiresTogether,
)
from boba.coercion.preconditions import (
    Default,
    NotNull,
    Nullable,
)
from boba.coercion.types import (
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
    "RequiresTogether",
    "SchemaContributor",
    "ValueCoercer",
]
