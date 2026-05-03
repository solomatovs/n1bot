"""DateTimeValue + DateTimeAdapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from boba.value.base import (
    ConfigValue,
    PythonValueAdapter,
)

__all__ = ["DateTimeAdapter", "DateTimeValue"]


@dataclass(frozen=True)
class DateTimeValue(ConfigValue):
    moment: datetime

    def unwrap(self) -> datetime:
        return self.moment

    def as_string(self) -> str:
        return self.moment.isoformat()


@dataclass(frozen=True)
class DateTimeAdapter(PythonValueAdapter):
    def try_wrap(self, value: Any) -> ConfigValue | None:
        # Важно: DateTimeAdapter проверяется ДО DateAdapter (datetime ⊂ date).
        if isinstance(value, datetime):
            return DateTimeValue(value)
        return None
