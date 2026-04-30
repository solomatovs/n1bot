"""BoolValue + BoolAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.confignext.value.base import (
    ConfigValue,
    PythonValueAdapter,
)

__all__ = ["BoolAdapter", "BoolValue"]


@dataclass(frozen=True)
class BoolValue(ConfigValue):
    flag: bool

    def unwrap(self) -> bool:
        return self.flag

    def as_bool(self) -> bool:
        return self.flag

    def as_string(self) -> str:
        return "true" if self.flag else "false"


@dataclass(frozen=True)
class BoolAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ConfigValue | None:
        # Важно: BoolAdapter проверяется ДО IntAdapter (bool — подкласс int).
        if isinstance(value, bool):
            return BoolValue(value)
        return None
