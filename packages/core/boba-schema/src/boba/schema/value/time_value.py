"""TimeValue + TimeAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Any

from boba.schema.value.base import (
    PythonValueAdapter,
    ScalarValue,
)

__all__ = ["TimeAdapter", "TimeValue"]


@dataclass(frozen=True)
class TimeValue(ScalarValue):
    point: time

    def unwrap(self) -> time:
        return self.point

    def as_string(self) -> str:
        return self.point.isoformat()


@dataclass(frozen=True)
class TimeAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ScalarValue | None:
        if isinstance(value, time):
            return TimeValue(value)
        return None
