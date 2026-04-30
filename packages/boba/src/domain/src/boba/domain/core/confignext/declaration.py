"""Декларация структуры: FieldKind / FieldSpec / MappingField / ListField / ObjectSchema.

Каждый FieldKind сам знает, как себя вычитать из FlatConfig
(`read_from(space, prefix)`); ObjectSchema умеет материализовать себя
целиком (`schema.materialize(space, prefix)`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from boba.domain.core.confignext.path import ConfigPath, NameSegment
from boba.domain.core.confignext.space import ConfigSpace
from boba.domain.core.confignext.validators import MISSING
from boba.domain.core.patterns import (
    Converter,
    ConverterInputError,
    MissingValueError,
)

__all__ = [
    "FieldKind",
    "FieldPathError",
    "FieldPathMissingError",
    "FieldSpec",
    "ListField",
    "MappingField",
    "MappingScalarField",
    "ObjectSchema",
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


@dataclass(frozen=True)
class MappingField(FieldKind, Generic[V]):
    """Динамическое поле: значение — Mapping[str, V]; ключи произвольны."""

    name: str
    value_schema: ObjectSchema[V]
    description: str = ""

    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> dict[str, V]:
        sub_prefix = prefix.join(NameSegment(self.name))
        result: dict[str, V] = {}
        seen: set[str] = set()
        for seg in space.child_segments(sub_prefix):
            key = seg.mapping_key()
            if key is None or key in seen:
                continue
            seen.add(key)
            try:
                result[key] = self.value_schema.materialize(space, sub_prefix.join(seg))
            except FieldPathError as exc:
                raise exc.with_parent_location(self.name, key) from exc
        return result


@dataclass(frozen=True)
class ListField(FieldKind, Generic[V]):
    """Индексированное поле: значение — tuple[V, ...]; элементы — объекты."""

    name: str
    item_schema: ObjectSchema[V]
    description: str = ""

    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> tuple[V, ...]:
        sub_prefix = prefix.join(NameSegment(self.name))
        items_by_index: dict[int, V] = {}
        for seg in space.child_segments(sub_prefix):
            i = seg.list_index()
            if i is None or i in items_by_index:
                continue
            try:
                items_by_index[i] = self.item_schema.materialize(
                    space, sub_prefix.join(seg)
                )
            except FieldPathError as exc:
                raise exc.with_parent_location(self.name, f"[{i}]") from exc
        return tuple(items_by_index[i] for i in sorted(items_by_index))


@dataclass(frozen=True)
class ScalarListField(FieldKind, Generic[T]):
    """Индексированный список скаляров: значение — tuple[T, ...].

    Параллель ListField для случая, когда элемент — примитив, а не объект:
      [chainlit] models = ["qwen3", "gemini"]
        → tuple[str, ...] через item_converter.
    """

    name: str
    item_converter: Converter[Any, T]
    description: str = ""

    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> tuple[T, ...]:
        sub_prefix = prefix.join(NameSegment(self.name))
        items_by_index: dict[int, T] = {}
        for seg in space.child_segments(sub_prefix):
            i = seg.list_index()
            if i is None or i in items_by_index:
                continue
            lookup = space.lookup(sub_prefix.join(seg))
            if not lookup.is_found():
                continue
            try:
                items_by_index[i] = self.item_converter.convert(lookup.value())
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(
                    exc, self.name, location=(f"[{i}]",)
                ) from exc
        return tuple(items_by_index[i] for i in sorted(items_by_index))


@dataclass(frozen=True)
class MappingScalarField(FieldKind, Generic[T]):
    """Динамический словарь скаляров: значение — Mapping[str, T].

    Параллель MappingField для случая, когда значение — примитив, а не объект:
      [tool_descriptions]
      kb_search = "..."
      html_outline = "..."
        → dict[str, str] через item_converter.
    """

    name: str
    item_converter: Converter[Any, T]
    description: str = ""

    def read_from(self, space: ConfigSpace, prefix: ConfigPath) -> dict[str, T]:
        sub_prefix = prefix.join(NameSegment(self.name))
        result: dict[str, T] = {}
        for seg in space.child_segments(sub_prefix):
            key = seg.mapping_key()
            if key is None or key in result:
                continue
            lookup = space.lookup(sub_prefix.join(seg))
            if not lookup.is_found():
                continue
            try:
                result[key] = self.item_converter.convert(lookup.value())
            except ConverterInputError as exc:
                raise FieldPathError.from_cause(
                    exc, self.name, location=(key,)
                ) from exc
        return result


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
