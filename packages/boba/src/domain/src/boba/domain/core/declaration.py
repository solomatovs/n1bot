"""Декларативные примитивы описания объекта."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeAlias, TypeVar

from boba.domain.core.schema import (
    ObjectWireSchema,
    ParamWireSchema,
    SchemaContributor,
)
from boba.domain.core.validators import MISSING, Pass, ValueConverter
from boba.patterns import (
    Converter,
    ConverterInputError,
    MissingValueError,
)

__all__ = [
    "FieldAddress",
    "FieldConverterError",
    "FieldMissingError",
    "FieldSpec",
    "ObjectSchema",
    "ParseObject",
    "validate_object",
]


# Адрес поля во внешнем namespace (ConfigKey и т.п.); этот слой не интерпретирует.
FieldAddress: TypeAlias = object


class FieldConverterError(ConverterInputError):
    """ConverterInputError со ссылкой на FieldSpec и адрес."""

    def __init__(
        self,
        message: str,
        *,
        field: FieldSpec[Any],
        key: FieldAddress | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.key = key


class FieldMissingError(FieldConverterError, MissingValueError):
    """FieldConverterError + MissingValueError-маркер."""


T = TypeVar("T")


@dataclass(frozen=True)
class FieldSpec(Generic[T]):
    """Декларация одного поля (name + Converter-цепочка + description)."""

    name: str
    converter: Converter[Any, T]
    description: str = ""

    def build_wire_schema(self) -> ParamWireSchema:
        """Собрать wire-описание поля через SchemaContributor."""
        schema = ParamWireSchema(property={"description": self.description})
        if isinstance(self.converter, SchemaContributor):
            self.converter.contribute(schema)
        return schema


@dataclass(frozen=True)
class ObjectSchema(Generic[T]):
    """Описание объекта: поля + invariants + factory + description."""

    fields: Sequence[FieldSpec[Any]]
    invariants: Converter[dict[str, Any], dict[str, Any]] = field(
        default_factory=Pass,
    )
    factory: Callable[..., T] = dict  # type: ignore[assignment]
    description: str = ""

    def build_wire_schema(self) -> ObjectWireSchema:
        """JSON-Schema-подобное описание объекта."""
        schema = ObjectWireSchema(description=self.description)
        for fld in self.fields:
            wire = fld.build_wire_schema()
            schema.properties[fld.name] = dict(wire.property)
            if wire.required:
                schema.required.append(fld.name)
        return schema


def validate_object(
    schema: ObjectSchema[T],
    read_raw: Callable[[str], object],
) -> T:
    """Читает поля через read_raw, конвертирует, применяет invariants, factory."""
    validated: dict[str, Any] = {}
    for fld in schema.fields:
        try:
            value = fld.converter.convert(read_raw(fld.name))
        except ConverterInputError as exc:
            raise _wrap_field_error(exc, fld) from exc
        if value is not MISSING:
            validated[fld.name] = value
    final = schema.invariants.convert(validated)
    return schema.factory(**final)


def _wrap_field_error(
    exc: ConverterInputError,
    fld: FieldSpec[Any],
) -> FieldConverterError:
    """Завернуть ошибку конвертера в field-aware подкласс."""
    message = f"field {fld.name!r}: {exc}"
    if isinstance(exc, MissingValueError):
        return FieldMissingError(message, field=fld)
    return FieldConverterError(message, field=fld)


class ParseObject(ValueConverter, SchemaContributor):
    """Конвертер для вложенного объекта по ObjectSchema; wire: type=object.

    Принимает Mapping (dict из JSON-args / TOML-таблицы), валидирует через
    validate_object и возвращает результат schema.factory(...). Удобно
    оборачивать в ParseList для массивов объектов.
    """

    def __init__(self, schema: ObjectSchema[Any]) -> None:
        self._schema = schema

    def _convert_value(self, value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise ConverterInputError(
                f"expected object, got {type(value).__name__}"
            )
        return validate_object(
            self._schema,
            lambda name: value.get(name, MISSING),
        )

    def contribute(self, schema: ParamWireSchema) -> None:
        wire = self._schema.build_wire_schema()
        schema.property["type"] = "object"
        if wire.properties:
            schema.property["properties"] = wire.properties
        if wire.required:
            schema.property["required"] = list(wire.required)
        if wire.description and "description" not in schema.property:
            schema.property["description"] = wire.description
