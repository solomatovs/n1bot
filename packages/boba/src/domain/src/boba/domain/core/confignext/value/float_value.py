"""FloatValue + FloatAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.confignext.value.base import (
    ConfigValue,
    PythonValueAdapter,
)

__all__ = ["FloatAdapter", "FloatValue"]


@dataclass(frozen=True)
class FloatValue(ConfigValue):
    number: float

    def unwrap(self) -> float:
        return self.number

    def as_float(self) -> float:
        return self.number

    def as_string(self) -> str:
        return str(self.number)


@dataclass(frozen=True)
class FloatAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ConfigValue | None:
        if isinstance(value, float):
            return FloatValue(value)
        return None
