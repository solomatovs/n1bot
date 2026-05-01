"""ConfigBundle / ConfigFactory: публичный фасад поверх FlatConfig + materialize."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from boba_next.declaration import (
    CollectionField,
    CollectionShape,
    FieldKind,
    FieldPathError,
    FieldSpec,
    IndexedShape,
    ItemReader,
    KeyedShape,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)
from boba_next.flat import FlatConfig, FlatConfigBuilder
from boba_next.path import (
    ConfigLookup,
    ConfigPath,
    ConfigSource,
    ConfigSpace,
    NameSegment,
    Segment,
)
from boba_next.validators import MISSING
from boba_next.value import ConfigValue
from boba_next.patterns import ConverterInputError

__all__ = ["ConfigBundle", "ConfigFactory", "ConfigMaterializer"]

T = TypeVar("T")


class ConfigMaterializer(Generic[T]):
    """Материализует ObjectSchema из ConfigSpace в DTO[T]."""

    def __init__(self, schema: ObjectSchema[T]) -> None:
        self._schema = schema

    def materialize(self, space: ConfigSpace, prefix: ConfigPath) -> T:
        validated: dict[str, Any] = {}
        for f in self._schema.fields:
            try:
                value = self._read_field(f, space, prefix)
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(exc, f.name) from exc

            if value is MISSING:
                continue
            validated[f.name] = value

        try:
            final = self._schema.invariants.convert(validated)
        except ConverterInputError as exc:
            raise FieldPathError.from_cause(exc, "<invariants>") from exc

        return self._schema.factory(**final)

    def _read_field(
        self,
        field: FieldKind,
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> Any:
        match field:
            case FieldSpec(name=name, converter=converter):
                path = prefix.join(NameSegment(name))
                lookup = space.lookup(path)
                raw = lookup.value() if lookup.is_found() else MISSING
                return converter.convert(raw)

            case CollectionField(name=name, reader=reader, shape=shape):
                return self._read_collection(name, reader, shape, space, prefix)

            case _:
                raise NotImplementedError(f"unknown FieldKind: {type(field).__name__}")

    def _read_collection(
        self,
        name: str,
        reader: ItemReader[Any],
        shape: CollectionShape[Any, Any, Any],
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> Any:
        sub_prefix = prefix.join(NameSegment(name))
        items: list[tuple[Any, Any]] = []
        seen: set[Any] = set()
        for key, seg, label in self._shape_entries(shape, space, sub_prefix):
            if key in seen:
                continue
            seen.add(key)
            try:
                value = self._read_item(reader, space, sub_prefix.join(seg))
            except FieldPathError as exc:
                raise exc.with_parent_location(name, label) from exc
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(exc, name, location=(label,)) from exc
            if value is not MISSING:
                items.append((key, value))
        return self._shape_assemble(shape, items)

    def _shape_entries(
        self,
        shape: CollectionShape[Any, Any, Any],
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> list[tuple[Any, Segment, str]]:
        match shape:
            case IndexedShape():
                out: list[tuple[Any, Segment, str]] = []
                for seg in space.child_segments(prefix):
                    i = seg.list_index()
                    if i is None:
                        continue
                    out.append((i, seg, f"[{i}]"))
                return out

            case KeyedShape():
                out2: list[tuple[Any, Segment, str]] = []
                for seg in space.child_segments(prefix):
                    k = seg.mapping_key()
                    if k is None:
                        continue
                    out2.append((k, seg, k))
                return out2

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
                # IndexedShape: tuple[V, ...] по возрастанию индекса.
                return tuple(v for _, v in sorted(items, key=lambda kv: kv[0]))

            case KeyedShape():
                # KeyedShape: dict[str, V].
                return dict(items)

            case _:
                raise NotImplementedError(
                    f"unknown CollectionShape: {type(shape).__name__}"
                )

    def _read_item(
        self,
        reader: ItemReader[Any],
        space: ConfigSpace,
        path: ConfigPath,
    ) -> Any:
        match reader:
            case ScalarItem(converter=converter):
                lookup = space.lookup(path)
                if not lookup.is_found():
                    return MISSING
                return converter.convert(lookup.value())

            case ObjectItem(schema=nested):
                # Рекурсия: сам ConfigMaterializer для вложенной схемы.
                return ConfigMaterializer(nested).materialize(space, path)

            case _:
                raise NotImplementedError(
                    f"unknown ItemReader: {type(reader).__name__}"
                )


@dataclass(frozen=True)
class ConfigBundle:
    """Публичный фасад над собранным FlatConfig.

    Способы использования:
      - bundle.materialize(SCHEMA, ConfigPath.parse("$ext.chromadb")) → DTO.
      - bundle.subtree(ConfigPath.parse("$ext.chromadb")) → плоский срез под префиксом.
      - bundle.lookup(ConfigPath.parse("$agent.max_iterations")) → ConfigLookup.
    """

    flat: FlatConfig

    def materialize(
        self,
        schema: ObjectSchema[T],
        prefix: ConfigPath,
    ) -> T:
        return ConfigMaterializer(schema).materialize(self.flat, prefix)

    def subtree(self, prefix: ConfigPath) -> Mapping[ConfigPath, ConfigValue]:
        return self.flat.subtree(prefix)

    def lookup(self, path: ConfigPath) -> ConfigLookup[ConfigValue]:
        return self.flat.lookup(path)

    def origin_of(self, path: ConfigPath) -> ConfigLookup[str]:
        return self.flat.origin_of(path)


class ConfigFactory:
    """Сборщик ConfigBundle из набора источников."""

    def __init__(self) -> None:
        self._sources: list[ConfigSource] = []

    def attach_sources(self, sources: Iterable[ConfigSource]) -> None:
        self._sources.extend(sources)

    def build(self) -> ConfigBundle:
        return ConfigBundle(flat=FlatConfigBuilder.from_sources(self._sources))
