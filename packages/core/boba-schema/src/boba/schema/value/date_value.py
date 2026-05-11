"""DateValue + DateAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from boba.schema.value.base import (
    PythonValueAdapter,
    ScalarValue,
)

__all__ = ["DateAdapter", "DateValue"]


@dataclass(frozen=True)
class DateValue(ScalarValue):
    day: date

    def unwrap(self) -> date:
        return self.day

    def as_string(self) -> str:
        return self.day.isoformat()


@dataclass(frozen=True)
class DateAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ScalarValue | None:
        if isinstance(value, date):
            return DateValue(value)
        return None
