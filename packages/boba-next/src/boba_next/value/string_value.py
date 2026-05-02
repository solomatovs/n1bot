"""StringValue + StringAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.patterns import ConverterInputError
from boba_next.value.base import (
    ConfigValue,
    PythonValueAdapter,
)

__all__ = ["StringAdapter", "StringValue"]


_TRUE_LITERALS: frozenset[str] = frozenset({"true", "1", "yes", "on"})
_FALSE_LITERALS: frozenset[str] = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class StringValue(ConfigValue):
    text: str

    def unwrap(self) -> str:
        return self.text

    def as_string(self) -> str:
        return self.text

    def as_int(self) -> int:
        try:
            return int(self.text.strip())
        except ValueError as exc:
            raise ConverterInputError(f"not a valid int: {self.text!r}") from exc

    def as_float(self) -> float:
        try:
            return float(self.text.strip())
        except ValueError as exc:
            raise ConverterInputError(f"not a valid float: {self.text!r}") from exc

    def as_bool(self) -> bool:
        normalized = self.text.strip().lower()
        if normalized in _TRUE_LITERALS:
            return True
        if normalized in _FALSE_LITERALS:
            return False
        raise ConverterInputError(f"not a valid bool: {self.text!r}")


@dataclass(frozen=True)
class StringAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ConfigValue | None:
        if isinstance(value, str):
            return StringValue(value)
        return None
