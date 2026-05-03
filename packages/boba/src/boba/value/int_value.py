"""IntValue + IntAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.value.base import (
    ConfigValue,
    PythonValueAdapter,
)

__all__ = ["IntAdapter", "IntValue"]


@dataclass(frozen=True)
class IntValue(ConfigValue):
    number: int

    def unwrap(self) -> int:
        return self.number

    def as_int(self) -> int:
        return self.number

    def as_float(self) -> float:
        return float(self.number)

    def as_string(self) -> str:
        return str(self.number)


@dataclass(frozen=True)
class IntAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ConfigValue | None:
        # bool — подкласс int; адаптер для bool должен срабатывать раньше.
        if not isinstance(value, bool) and isinstance(value, int):
            return IntValue(value)
        return None
