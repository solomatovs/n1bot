"""NullValue + NullAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.schema.value.base import (
    PythonValueAdapter,
    ScalarValue,
)

__all__ = ["NullAdapter", "NullValue"]


@dataclass(frozen=True)
class NullValue(ScalarValue):
    """Явный null из источника (TOML не имеет null, но env/cli могут)."""

    def unwrap(self) -> None:
        return None

    # as_int/as_float/as_bool/as_string наследуются из базы → бросают ошибку.


@dataclass(frozen=True)
class NullAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ScalarValue | None:
        if value is None:
            return NullValue()
        return None
