"""Type-конвертеры:
- Parse* — coercion через полиморфные as_* на ConfigValue;
- Is*    — строгая проверка конкретного подтипа ConfigValue.

Каждый Parse*/Is* реализует SchemaContributor: пишет JSON-Schema `type` в prop.
"""

from __future__ import annotations

from typing import Any

from boba.coercion.base import SchemaContributor, ValueCoercer
from boba.patterns import ConverterInputError
from boba.value import (
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
    "ParseCsvList",
    "ParseFloat",
    "ParseInt",
    "ParseString",
]


class ParseString(ValueCoercer, SchemaContributor):
    """Привести любое значение к str."""

    def _apply_value(self, value: ConfigValue) -> str:
        return value.as_string()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "string"


class ParseInt(ValueCoercer, SchemaContributor):
    """Привести значение к int (bool отвергается)."""

    def _apply_value(self, value: ConfigValue) -> int:
        return value.as_int()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "integer"


class ParseFloat(ValueCoercer, SchemaContributor):
    """Привести значение к float (bool отвергается)."""

    def _apply_value(self, value: ConfigValue) -> float:
        return value.as_float()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "number"


class ParseBool(ValueCoercer, SchemaContributor):
    """Привести значение к bool."""

    def _apply_value(self, value: ConfigValue) -> bool:
        return value.as_bool()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "boolean"


class IsString(ValueCoercer, SchemaContributor):
    """Строго StringValue (без coercion)."""

    def _apply_value(self, value: ConfigValue) -> str:
        if not isinstance(value, StringValue):
            raise ConverterInputError(
                f"ожидалась строка, получено {type(value).__name__}"
            )
        return value.as_string()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "string"


class IsInt(ValueCoercer, SchemaContributor):
    """Строго IntValue (без coercion; bool отвергается)."""

    def _apply_value(self, value: ConfigValue) -> int:
        if not isinstance(value, IntValue):
            raise ConverterInputError(
                f"ожидалось целое число, получено {type(value).__name__}"
            )
        return value.as_int()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "integer"


class IsNumber(ValueCoercer, SchemaContributor):
    """Строго IntValue или FloatValue (bool отвергается)."""

    def _apply_value(self, value: ConfigValue) -> int | float:
        if not isinstance(value, (IntValue, FloatValue)):
            raise ConverterInputError(
                f"ожидалось число, получено {type(value).__name__}"
            )
        return value.unwrap()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "number"


class IsBool(ValueCoercer, SchemaContributor):
    """Строго BoolValue."""

    def _apply_value(self, value: ConfigValue) -> bool:
        if not isinstance(value, BoolValue):
            raise ConverterInputError(f"ожидался bool, получено {type(value).__name__}")
        return value.as_bool()

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "boolean"


class ParseCsvList(ValueCoercer, SchemaContributor):
    """Привести значение к list[str] (split по запятой); wire: type=array."""

    def _apply_value(self, value: ConfigValue) -> list[str]:
        raw = value.unwrap()
        if isinstance(raw, list):
            return [str(item) for item in raw if item is not None and str(item) != ""]
        if isinstance(raw, str):
            return [item.strip() for item in raw.split(",") if item.strip()]
        raise ConverterInputError(
            f"cannot convert {type(raw).__name__} to list[str]",
        )

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["type"] = "array"
        prop["items"] = {"type": "string"}
