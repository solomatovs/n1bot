"""
ToolArgsBuilder выполняет роль конвертера аргументов, которые передает LLM
при вызове инструмента, в типизированный DTO, который может использоваться
    1. валидирует Mapping[str, Any] (переданный от llm)
    2. создает DTO из Mapping[str, Any]
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from boba.coercion import MISSING
from boba.declaration import (
    CollectionField,
    CollectionShape,
    FieldKind,
    FieldPathError,
    FieldPathMissingError,
    FieldSpec,
    IndexedShape,
    ItemReader,
    KeyedShape,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)
from boba.patterns import ConverterInputError

__all__ = ["ToolArgsBuilder"]


T = TypeVar("T")


class ToolArgsBuilder(Generic[T]):
    """Валидирует Mapping[str, Any] через ObjectSchema в типизированный DTO[T]."""

    def __init__(self, schema: ObjectSchema[T]) -> None:
        self._schema = schema

    def build(self, raw: Mapping[str, Any]) -> T:
        validated: dict[str, Any] = {}
        for f in self._schema.fields:
            try:
                value = self._read_field(f, raw)
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(exc, f.name) from exc

            if value is MISSING:
                if isinstance(f, FieldSpec) and f.required:
                    raise FieldPathMissingError(
                        f"field {f.name!r}: required value is missing",
                        field_name=f.name,
                    )
                continue
            validated[f.name] = value

        try:
            final = self._schema.invariants.apply(validated)
        except ConverterInputError as exc:
            raise FieldPathError.from_cause(exc, "<invariants>") from exc

        return self._schema.factory(**final)

    def _read_field(self, field: FieldKind, raw: Mapping[str, Any]) -> Any:
        match field:
            case FieldSpec(name=name, coercer=coercer):
                return coercer.apply(raw.get(name, MISSING))

            case CollectionField(name=name, reader=reader, shape=shape):
                return self._read_collection(name, reader, shape, raw)

            case _:
                raise NotImplementedError(f"unknown FieldKind: {type(field).__name__}")

    def _read_collection(
        self,
        name: str,
        reader: ItemReader[Any],
        shape: CollectionShape[Any, Any, Any],
        raw: Mapping[str, Any],
    ) -> Any:
        sub = raw.get(name, MISSING)
        if sub is MISSING:
            return self._shape_assemble(shape, [])

        items: list[tuple[Any, Any]] = []
        for key, item_raw, label in self._iter_collection(name, sub, shape):
            try:
                value = self._read_item(reader, item_raw)
            except FieldPathError as exc:
                raise exc.with_parent_location(name, label) from exc
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(exc, name, location=(label,)) from exc
            if value is not MISSING:
                items.append((key, value))
        return self._shape_assemble(shape, items)

    def _iter_collection(
        self,
        name: str,
        sub: Any,
        shape: CollectionShape[Any, Any, Any],
    ) -> list[tuple[Any, Any, str]]:
        del name  # имя поля прицепит обёртывающий FieldPathError.from_cause
        match shape:
            case IndexedShape():
                if not isinstance(sub, list):
                    raise ConverterInputError(
                        f"expected list, got {type(sub).__name__}"
                    )
                return [(i, item, f"[{i}]") for i, item in enumerate(sub)]

            case KeyedShape():
                if not isinstance(sub, Mapping):
                    raise ConverterInputError(
                        f"expected mapping, got {type(sub).__name__}"
                    )
                return [(k, v, k) for k, v in sub.items()]

            case _:
                raise NotImplementedError(
                    f"unknown CollectionShape: {type(shape).__name__}"
                )

    def _shape_assemble(
        self,
        shape: CollectionShape[Any, Any, Any],
        items: list[tuple[Any, Any]],
    ) -> Any:
        match shape:
            case IndexedShape():
                return tuple(v for _, v in sorted(items, key=lambda kv: kv[0]))

            case KeyedShape():
                return dict(items)

            case _:
                raise NotImplementedError(
                    f"unknown CollectionShape: {type(shape).__name__}"
                )

    def _read_item(self, reader: ItemReader[Any], raw: Any) -> Any:
        match reader:
            case ScalarItem(coercer=coercer):
                if raw is MISSING:
                    return MISSING
                return coercer.apply(raw)

            case ObjectItem(schema=nested):
                if not isinstance(raw, Mapping):
                    raise ConverterInputError(
                        f"expected mapping, got {type(raw).__name__}"
                    )
                return ToolArgsBuilder(nested).build(raw)

            case _:
                raise NotImplementedError(
                    f"unknown ItemReader: {type(reader).__name__}"
                )
