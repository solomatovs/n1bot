"""Type-конвертеры:
- Parse* — coercion через полиморфные as_* на ConfigValue;
- Is*    — строгая проверка конкретного подтипа ConfigValue.
"""

from __future__ import annotations

from boba.patterns import ConverterInputError
from boba_next.validators.base import ValueConverter
from boba_next.value import (
    BoolValue,
    ConfigValue,
    FloatValue,
    IntValue,
    StringValue,
)

__all__ = [
    "IsBool",
    "IsInt",
    "IsNumber",
    "IsString",
    "ParseBool",
    "ParseFloat",
    "ParseInt",
    "ParseString",
]


# ──────────────────── Parse* — coercion через as_* ────────────────────


class ParseString(ValueConverter):
    """Привести любое значение к str."""

    def _convert_value(self, value: ConfigValue) -> str:
        return value.as_string()


class ParseInt(ValueConverter):
    """Привести значение к int (bool отвергается)."""

    def _convert_value(self, value: ConfigValue) -> int:
        return value.as_int()


class ParseFloat(ValueConverter):
    """Привести значение к float (bool отвергается)."""

    def _convert_value(self, value: ConfigValue) -> float:
        return value.as_float()


class ParseBool(ValueConverter):
    """Привести значение к bool."""

    def _convert_value(self, value: ConfigValue) -> bool:
        return value.as_bool()


# ──────────────────── Is* — строгий type-guard ────────────────────


class IsString(ValueConverter):
    """Строго StringValue (без coercion)."""

    def _convert_value(self, value: ConfigValue) -> str:
        if not isinstance(value, StringValue):
            raise ConverterInputError(
                f"ожидалась строка, получено {type(value).__name__}"
            )
        return value.as_string()


class IsInt(ValueConverter):
    """Строго IntValue (без coercion; bool отвергается)."""

    def _convert_value(self, value: ConfigValue) -> int:
        if not isinstance(value, IntValue):
            raise ConverterInputError(
                f"ожидалось целое число, получено {type(value).__name__}"
            )
        return value.as_int()


class IsNumber(ValueConverter):
    """Строго IntValue или FloatValue (bool отвергается)."""

    def _convert_value(self, value: ConfigValue) -> int | float:
        if not isinstance(value, (IntValue, FloatValue)):
            raise ConverterInputError(
                f"ожидалось число, получено {type(value).__name__}"
            )
        return value.unwrap()


class IsBool(ValueConverter):
    """Строго BoolValue."""

    def _convert_value(self, value: ConfigValue) -> bool:
        if not isinstance(value, BoolValue):
            raise ConverterInputError(f"ожидался bool, получено {type(value).__name__}")
        return value.as_bool()
