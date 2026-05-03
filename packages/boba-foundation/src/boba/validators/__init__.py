"""confignext.validators: конвертеры/валидаторы по группам.

- base.py            — MISSING, Pass, ChainConverter, ValueConverter (база).
- preconditions.py   — NotNull, Default, Nullable (наличие значения).
- types.py           — Parse* (coercion) и Is* (строгий type-guard).
- constraints.py     — OneOf / Min*/Max* / NonEmpty (ограничения).
- invariants.py      — MutuallyExclusive / RequiresTogether / Ordered (object-level).
"""

from boba.validators.base import (
    MISSING,
    ChainConverter,
    Pass,
    ValueConverter,
)
from boba.validators.constraints import (
    MaxLength,
    MaxValue,
    MinLength,
    MinValue,
    NonEmpty,
    OneOf,
)
from boba.validators.invariants import (
    MutuallyExclusive,
    Ordered,
    RequiresTogether,
)
from boba.validators.preconditions import (
    Default,
    NotNull,
    Nullable,
)
from boba.validators.types import (
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
    "ChainConverter",
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
    "ValueConverter",
]
