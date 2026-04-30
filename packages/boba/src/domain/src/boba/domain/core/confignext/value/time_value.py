"""TimeValue + TimeAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from boba.domain.core.confignext.value.base import (
    ConfigValue,
    PythonValueAdapter,
)

__all__ = ["TimeAdapter", "TimeValue"]


@dataclass(frozen=True)
class TimeValue(ConfigValue):
    point: time

    def unwrap(self) -> time:
        return self.point

    def as_string(self) -> str:
        return self.point.isoformat()


@dataclass(frozen=True)
class TimeAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ConfigValue | None:
        if isinstance(value, time):
            return TimeValue(value)
        return None
