"""FloatValue + FloatAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.schema.value.base import (
    PythonValueAdapter,
    ScalarValue,
)

__all__ = ["FloatAdapter", "FloatValue"]


@dataclass(frozen=True)
class FloatValue(ScalarValue):
    number: float

    def unwrap(self) -> float:
        return self.number

    def as_float(self) -> float:
        return self.number

    def as_string(self) -> str:
        return str(self.number)


@dataclass(frozen=True)
class FloatAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ScalarValue | None:
        if isinstance(value, float):
            return FloatValue(value)
        return None
