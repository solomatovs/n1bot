"""Адресация и резолвинг конфигурационных значений."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any, ClassVar, Generic, TypeVar

from boba.domain.core.declaration import (
    FieldConverterError,
    FieldMissingError,
    FieldSpec,
    ObjectSchema,
    validate_object,
)
from boba.domain.core.patterns import ConverterInputError, StrId
from boba.domain.core.validators import MISSING

__all__ = [
    "ChainedConfigResolver",
    "ConfigKey",
    "ConfigSection",
    "ConfigSource",
    "read_field",
]


T = TypeVar("T")


class ConfigKey:
    """Иерархический source-agnostic идентификатор поля конфига."""

    _MIN_PARTS: ClassVar[int] = 1

    __slots__ = ("_parts",)

    def __init__(self, *parts: str) -> None:
        if len(parts) < self._MIN_PARTS:
            raise ValueError(
                f"ConfigKey requires at least {self._MIN_PARTS} part; "
                f"got {parts!r}"
            )
        for p in parts:
            if not p:
                raise ValueError(
                    f"ConfigKey part must be non-empty string; got {parts!r}"
                )
            if not p.replace("_", "").isalnum():
                raise ValueError(
                    f"ConfigKey part {p!r} must match [A-Za-z0-9_]; "
                    f"got {parts!r}"
                )
        self._parts = parts

    @property
    def parts(self) -> tuple[str, ...]:
        return self._parts

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ConfigKey) and self._parts == other._parts

    def __hash__(self) -> int:
        return hash(self._parts)

    def __repr__(self) -> str:
        inside = ", ".join(repr(p) for p in self._parts)
        return f"ConfigKey({inside})"


class ConfigSource(ABC):
    """Источник сырых значений по ConfigKey; None = «пропусти»."""

    def bind_schema(
        self,
        items: Iterable[tuple[ConfigKey, FieldSpec[Any]]],
    ) -> None:
        """Получить полный набор ожидаемых ключей со схемой; default — no-op."""
        del items

    def describe(self, key: ConfigKey) -> str:
        """Operator-readable рецепт задания ключа; пустая строка — не умеет."""
        del key
        return ""

    @abstractmethod
    def resolve(self, key: ConfigKey) -> object | None: ...


class ChainedConfigResolver:
    """Опрашивает источники по порядку; первый non-None выигрывает."""

    def __init__(self, sources: Sequence[ConfigSource]) -> None:
        self._sources = list(sources)

    @property
    def sources(self) -> Sequence[ConfigSource]:
        """Read-only view списка источников."""
        return tuple(self._sources)

    def resolve(self, key: ConfigKey) -> object | None:
        for source in self._sources:
            value = source.resolve(key)
            if value is not None:
                return value
        return None


def read_field(
    key: ConfigKey,
    field: FieldSpec[T],
    resolver: ChainedConfigResolver,
) -> T:
    """Ad-hoc чтение одного поля; None от резолвера → MISSING."""
    raw: object | None = resolver.resolve(key)
    value: Any = MISSING if raw is None else raw
    try:
        return field.converter.convert(value)
    except ConverterInputError as exc:
        raise ConverterInputError(
            f"Config field {key!r}: {exc}"
        ) from exc


class ConfigSection(ABC, Generic[T]):
    """Декларация одной секции конфига (ObjectSchema + namespace)."""

    id: ClassVar[StrId]
    namespace: ClassVar[tuple[str, ...]]
    schema: ClassVar[ObjectSchema[Any]]

    def build(self, resolver: ChainedConfigResolver) -> T:
        """Прочитать поля schema через резолвер и собрать DTO."""

        def _read_raw(name: str) -> object:
            key = ConfigKey(*self.namespace, name)
            raw = resolver.resolve(key)
            return MISSING if raw is None else raw

        try:
            return validate_object(self.schema, _read_raw)
        except FieldConverterError as exc:
            raise self._attach_key(exc) from exc.__cause__

    def _attach_key(self, exc: FieldConverterError) -> FieldConverterError:
        """Дописать ConfigKey к ошибке валидации поля."""
        if exc.key is not None:
            return exc
        full_key = ConfigKey(*self.namespace, exc.field.name)
        if isinstance(exc, FieldMissingError):
            return FieldMissingError(str(exc), field=exc.field, key=full_key)
        return FieldConverterError(str(exc), field=exc.field, key=full_key)
