from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from boba.domain.core.confignext.path import (
    ConfigLookup,
    ConfigPath,
    ConfigSpace,
    Found,
    NameSegment,
    NotFound,
    Segment,
)
from boba.domain.core.confignext.validators import MISSING
from boba.domain.core.patterns import (
    Converter,
    ConverterInputError,
    MissingValueError,
)

__all__ = [
    "CollectionField",
    "CollectionShape",
    "FieldKind",
    "FieldPathError",
    "FieldPathMissingError",
    "FieldSpec",
    "IndexedShape",
    "ItemReader",
    "KeyedShape",
    "ListField",
    "MappingField",
    "MappingScalarField",
    "ObjectItem",
    "ObjectSchema",
    "ScalarItem",
    "ScalarListField",
]


class FieldPathError(ConverterInputError):
    """ConverterInputError с привязкой к имени поля и пройденному пути."""

    def __init__(
        self,
        message: str,
        *,
        field_name: str,
        location: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.field_name = field_name
        self.location = location

    @classmethod
    def from_cause(
        cls,
        exc: ConverterInputError,
        field_name: str,
        location: tuple[str, ...] = (),
    ) -> FieldPathError:
        """Завернуть произвольный ConverterInputError в FieldPathError.

        Подкласс с маркером MissingValueError остаётся MissingValueError —
        возвращается FieldPathMissingError.
        """
        if isinstance(exc, FieldPathError):
            return exc
        message = f"field {cls._render_location(field_name, location)!r}: {exc}"
        target = (
            FieldPathMissingError
            if isinstance(exc, MissingValueError)
            else FieldPathError
        )
        return target(message, field_name=field_name, location=location)

    def with_parent_location(
        self,
        parent_field: str,
        child: str,
    ) -> FieldPathError:
        """Завернуть текущую ошибку как происходящую внутри parent_field/child."""
        new_location = (child, *self.location)
        new_message = (
            f"field "
            f"{self._render_location(parent_field, new_location)!r}: "
            f"{self._strip_field_prefix(str(self))}"
        )
        return type(self)(
            new_message,
            field_name=parent_field,
            location=new_location,
        )

    @staticmethod
    def _render_location(field_name: str, location: tuple[str, ...]) -> str:
        if not location:
            return field_name
        return field_name + "." + ".".join(location)

    @staticmethod
    def _strip_field_prefix(message: str) -> str:
        """Снять «field 'xxx': » если есть."""
        if message.startswith("field '"):
            end = message.find("': ")
            if end != -1:
                return message[end + 3 :]
        return message


class FieldPathMissingError(FieldPathError, MissingValueError):
    """FieldPathError + MissingValueError-маркер."""


T = TypeVar("T")
V = TypeVar("V")
K = TypeVar("K")
R = TypeVar("R")


class FieldKind(ABC):
    """Базовый класс полей ObjectSchema; знает, как себя вычитать."""

    name: str
    description: str

    @abstractmethod
    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> Any:
        """Прочитать значение поля из space относительно prefix.

        Возвращает MISSING, если поле отсутствует и нет default'а.
        Бросает FieldPathError при ошибке валидации.
        """


@dataclass(frozen=True)
class FieldSpec(FieldKind, Generic[T]):
    """Скалярное поле: name + Converter-цепочка."""

    name: str
    converter: Converter[Any, T]
    description: str = ""

    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> Any:
        path = prefix.join(NameSegment(self.name))
        lookup = space.lookup(path)
        raw = lookup.value() if lookup.is_found() else MISSING
        return self.converter.convert(raw)


class ItemReader(ABC, Generic[V]):
    """Чтение одного элемента коллекции по абсолютному пути."""

    @abstractmethod
    def read(self, space: ConfigSpace, path: ConfigPath) -> ConfigLookup[V]:
        """Found(V) — есть значение; NotFound — пропустить элемент."""


class CollectionShape(ABC, Generic[K, V, R]):
    """Форма коллекции: какие сегменты считаются элементами и как собрать результат."""

    @abstractmethod
    def entries(
        self,
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> Iterator[tuple[K, Segment, str]]:
        """yields (ключ, сегмент, label-для-ошибок); фильтрует «не свои» сегменты."""

    @abstractmethod
    def assemble(self, items: list[tuple[K, V]]) -> R:
        """Собрать итог из накопленных пар (ключ, значение)."""


@dataclass(frozen=True)
class ScalarItem(ItemReader[T], Generic[T]):
    """Скаляр: lookup → converter."""

    converter: Converter[Any, T]

    def read(self, space: ConfigSpace, path: ConfigPath) -> ConfigLookup[T]:
        lookup = space.lookup(path)
        if not lookup.is_found():
            return NotFound()
        return Found(self.converter.convert(lookup.value()))


@dataclass(frozen=True)
class ObjectItem(ItemReader[V], Generic[V]):
    """Объект: рекурсивный materialize по схеме."""

    schema: ObjectSchema[V]

    def read(self, space: ConfigSpace, path: ConfigPath) -> ConfigLookup[V]:
        return Found(self.schema.materialize(space, path))


@dataclass(frozen=True)
class IndexedShape(CollectionShape[int, V, tuple[V, ...]], Generic[V]):
    """tuple[V, ...] по `[i]`; результат отсортирован по индексу."""

    def entries(
        self,
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> Iterator[tuple[int, Segment, str]]:
        for seg in space.child_segments(prefix):
            i = seg.list_index()
            if i is None:
                continue
            yield i, seg, f"[{i}]"

    def assemble(self, items: list[tuple[int, V]]) -> tuple[V, ...]:
        return tuple(v for _, v in sorted(items, key=lambda kv: kv[0]))


@dataclass(frozen=True)
class KeyedShape(CollectionShape[str, V, dict[str, V]], Generic[V]):
    """dict[str, V] по mapping-ключам."""

    def entries(
        self,
        space: ConfigSpace,
        prefix: ConfigPath,
    ) -> Iterator[tuple[str, Segment, str]]:
        for seg in space.child_segments(prefix):
            k = seg.mapping_key()
            if k is None:
                continue
            yield k, seg, k

    def assemble(self, items: list[tuple[str, V]]) -> dict[str, V]:
        return dict(items)


@dataclass(frozen=True)
class CollectionField(FieldKind, Generic[K, V, R]):
    """Поле-коллекция: ItemReader (чтение элемента) × CollectionShape (сборка)."""

    name: str
    reader: ItemReader[V]
    shape: CollectionShape[K, V, R]
    description: str = ""

    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> R:
        sub_prefix = prefix.join(NameSegment(self.name))
        items: list[tuple[K, V]] = []
        seen: set[K] = set()
        for key, seg, label in self.shape.entries(space, sub_prefix):
            if key in seen:
                continue
            seen.add(key)
            try:
                lookup = self.reader.read(space, sub_prefix.join(seg))
            except FieldPathError as exc:
                raise exc.with_parent_location(self.name, label) from exc
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(
                    exc, self.name, location=(label,)
                ) from exc
            if lookup.is_found():
                items.append((key, lookup.value()))
        return self.shape.assemble(items)


def MappingField(  # noqa: N802 — публичный API в PascalCase
    name: str,
    value_schema: ObjectSchema[V],
    description: str = "",
) -> CollectionField[str, V, dict[str, V]]:
    """Динамический словарь объектов: ключи произвольны, значения — DTO по схеме."""
    return CollectionField(
        name=name,
        reader=ObjectItem(value_schema),
        shape=KeyedShape(),
        description=description,
    )


def ListField(  # noqa: N802 — публичный API в PascalCase
    name: str,
    item_schema: ObjectSchema[V],
    description: str = "",
) -> CollectionField[int, V, tuple[V, ...]]:
    """Индексированный список объектов: tuple[V, ...] по схеме элемента."""
    return CollectionField(
        name=name,
        reader=ObjectItem(item_schema),
        shape=IndexedShape(),
        description=description,
    )


def ScalarListField(  # noqa: N802 — публичный API в PascalCase
    name: str,
    item_converter: Converter[Any, T],
    description: str = "",
) -> CollectionField[int, T, tuple[T, ...]]:
    """Индексированный список скаляров: tuple[T, ...] через item_converter."""
    return CollectionField(
        name=name,
        reader=ScalarItem(item_converter),
        shape=IndexedShape(),
        description=description,
    )


def MappingScalarField(  # noqa: N802 — публичный API в PascalCase
    name: str,
    item_converter: Converter[Any, T],
    description: str = "",
) -> CollectionField[str, T, dict[str, T]]:
    """Динамический словарь скаляров: dict[str, T] через item_converter."""
    return CollectionField(
        name=name,
        reader=ScalarItem(item_converter),
        shape=KeyedShape(),
        description=description,
    )


class _PassDict(Converter[dict[str, Any], dict[str, Any]]):
    """No-op invariants по умолчанию для ObjectSchema."""

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        return value


@dataclass(frozen=True)
class ObjectSchema(Generic[T]):
    """Схема объекта: упорядоченная коллекция полей + invariants + factory."""

    fields: Sequence[FieldKind]
    invariants: Converter[dict[str, Any], dict[str, Any]] = field(
        default_factory=lambda: _PassDict(),
    )
    factory: Callable[..., T] = dict  # type: ignore[assignment]
    description: str = ""

    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def materialize(self, space: ConfigSpace, prefix: ConfigPath) -> T:
        """
        Собрать DTO: каждое поле само вычитывает себя через read_from.
        """
        validated: dict[str, Any] = {}
        for f in self.fields:
            try:
                value = f.read_from(space, prefix)
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(exc, f.name) from exc

            if value is MISSING:
                continue

            validated[f.name] = value

        try:
            final = self.invariants.convert(validated)
        except ConverterInputError as exc:
            raise FieldPathError.from_cause(exc, "<invariants>") from exc

        return self.factory(**final)
