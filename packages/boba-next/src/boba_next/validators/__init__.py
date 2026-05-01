"""confignext.validators: конвертеры/валидаторы по группам.

- base.py            — MISSING, Pass, ChainConverter, ValueConverter (база).
- preconditions.py   — Required, Default, Nullable (наличие значения).
- types.py           — Parse* (coercion) и Is* (строгий type-guard).
- constraints.py     — OneOf / Min*/Max* / NonEmpty (ограничения).
- invariants.py      — MutuallyExclusive / RequiresTogether / Ordered (object-level).
"""

from boba_next.validators.base import (
    MISSING,
    ChainConverter,
    Pass,
    ValueConverter,
)
from boba_next.validators.constraints import (
    MaxLength,
    MaxValue,
    MinLength,
    MinValue,
    NonEmpty,
    OneOf,
)
from boba_next.validators.invariants import (
    MutuallyExclusive,
    Ordered,
    RequiresTogether,
)
from boba_next.validators.preconditions import (
    Default,
    Nullable,
    Required,
)
from boba_next.validators.types import (
    IsBool,
    IsInt,
    IsNumber,
    IsString,
    ParseBool,
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
    "Nullable",
    "OneOf",
    "Ordered",
    "ParseBool",
    "ParseFloat",
    "ParseInt",
    "ParseString",
    "Pass",
    "Required",
    "RequiresTogether",
    "ValueConverter",
]
