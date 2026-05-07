"""ConfigBundle / ConfigFactory: публичный фасад поверх FlatConfig + materialize."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from boba.coercion import MISSING
from boba.config.flat import FlatConfig
from boba.config.path import (
    ConfigPath,
    ConfigSource,
    ConfigSpace,
    NameSegment,
    Segment,
)
from boba.config.refs import ReferenceResolver
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
    NestedField,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)
from boba.patterns import (
    ConverterInputError,
    FoldFactory,
    PrioritySource,
    StrId,
)
from boba.value import ConfigValue

__all__ = ["ConfigBundle", "ConfigBundleFactory", "FlatConfigMaterializer", "PathLike"]

T = TypeVar("T")

PathLike = str | ConfigPath
"""Аргумент-путь в публичном API: либо текст ('app.core'), либо ConfigPath."""


def _as_path(p: PathLike) -> ConfigPath:
    return p if isinstance(p, ConfigPath) else ConfigPath.parse(p)


class FlatConfigMaterializer(Generic[T]):
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

    def _read_field(
        self,
        field: FieldKind,
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> Any:
        match field:
            case FieldSpec(name=name, coercer=coercer):
                path = prefix.join(NameSegment(name))
                lookup = space.lookup(path)
                raw = lookup.value() if lookup.is_found() else MISSING
                return coercer.apply(raw)

            case CollectionField(name=name, reader=reader, shape=shape):
                return self._read_collection(name, reader, shape, space, prefix)

            case NestedField(name=name, schema=nested):
                return FlatConfigMaterializer(nested).materialize(
                    space, prefix.join(NameSegment(name)),
                )

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
            case ScalarItem(coercer=coercer):
                lookup = space.lookup(path)
                if not lookup.is_found():
                    return MISSING
                return coercer.apply(lookup.value())

            case ObjectItem(schema=nested):
                # Рекурсия: сам ConfigMaterializer для вложенной схемы.
                return FlatConfigMaterializer(nested).materialize(space, path)

            case _:
                raise NotImplementedError(
                    f"unknown ItemReader: {type(reader).__name__}"
                )


@dataclass(frozen=True)
class ConfigBundle:
    """Публичный фасад над собранным FlatConfig.

    Пути принимаются как строкой ('ext.chromadb'), так и `ConfigPath` —
    нормализация прячется внутри. В composition root импортировать
    `ConfigPath` не требуется.

    Способы использования:
      - bundle.get(MyConfig, "ext.chromadb") → DTO (DTO.SCHEMA).
      - bundle.materialize("ext.chromadb", SCHEMA) → DTO (low-level).
      - ConfigBundle.from_sources([...]) — удобный one-shot конструктор.
    """

    flat: FlatConfig

    @classmethod
    def from_sources(cls, sources: Iterable[ConfigSource]) -> ConfigBundle:
        """Удобный one-shot: собрать ConfigBundle из набора источников."""
        f = ConfigBundleFactory()
        f.attach_sources(sources)
        return f.build()

    def materialize(
        self,
        prefix: PathLike,
        schema: ObjectSchema[T],
    ) -> T:
        return FlatConfigMaterializer(schema).materialize(self.flat, _as_path(prefix))

    def get(self, dto_cls: type[T], prefix: PathLike) -> T:
        """Материализовать DTO по его `ClassVar SCHEMA`.

        Соглашение: каждый config-DTO объявляет
        ``SCHEMA: ClassVar[ObjectSchema[Self]]``. Это делает DTO
        самодостаточным — пользователь импортирует только класс, без
        отдельного `*_SCHEMA`-имени.
        """
        schema = getattr(dto_cls, "SCHEMA", None)
        if not isinstance(schema, ObjectSchema):
            msg = (
                f"{dto_cls.__name__} must declare "
                f"`SCHEMA: ClassVar[ObjectSchema[{dto_cls.__name__}]]`"
            )
            raise TypeError(msg)
        return self.materialize(prefix, schema)


@dataclass
class _MergeState:
    """Промежуточное состояние сборки: накопленные values + origins."""

    values: dict[ConfigPath, ConfigValue] = field(default_factory=dict)
    origins: dict[ConfigPath, str] = field(default_factory=dict)


class _SourceReducer(PrioritySource[StrId, _MergeState]):
    """Адаптер: один ConfigSource как стадия FoldFactory."""

    def __init__(self, source: ConfigSource) -> None:
        self._src = source
        self._id = StrId(source.name())

    @property
    def source(self) -> ConfigSource:
        return self._src

    def id(self) -> StrId:
        return self._id

    def priority(self) -> int:
        return self._src.priority()

    def apply(self, state: _MergeState) -> _MergeState:
        name = self._src.name()
        for path, value in self._src.load().items():
            state.values[path] = value
            state.origins[path] = name
        return state


class ConfigBundleFactory(FoldFactory[StrId, _MergeState, ConfigBundle]):
    """Источники → ConfigBundle. FoldFactory: каждый source — стадия мержа.

    Сборка: source'ы сортируются по priority и последовательно сливаются в
    единый snapshot (last-wins), затем заворачиваются в FlatConfig + ConfigBundle.
    Pure factory: build() каждый раз даёт свежий ConfigBundle.
    """

    def initial(self) -> _MergeState:
        return _MergeState()

    def finalize(self, state: _MergeState) -> ConfigBundle:
        # Между fold источников и созданием FlatConfig разрешаем @{...}-ссылки.
        # После resolve в значениях ссылок не остаётся, origins содержит
        # цепочку шагов разрешения (длина 1 — без ссылок).
        values, origins = ReferenceResolver().resolve(state.values, state.origins)
        return ConfigBundle(flat=FlatConfig(values=values, origins=origins))

    def attach_sources(self, sources: Iterable[ConfigSource]) -> None:
        """Удобный helper: завернуть source'ы в reducer'ы и зарегистрировать."""
        for src in sources:
            self.register(_SourceReducer(src))
