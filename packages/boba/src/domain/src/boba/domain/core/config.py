"""Абстракции унифицированной работы с конфигурацией."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from boba.domain.core.patterns import Converter, ConverterInputError

__all__ = [
    "BoolConverter",
    "ChainedConfigResolver",
    "ConfigSource",
    "CsvListConverter",
    "FieldSpec",
    "FloatConverter",
    "IntConverter",
    "MappingMappingConverter",
    "StrConverter",
]


T = TypeVar("T")


@dataclass(frozen=True)
class FieldSpec(Generic[T]):
    """Одно поле конфига: ``key`` + converter + default."""

    key: str
    converter: Converter[object, T]
    default: T | None = None

    def read(self, resolver: ChainedConfigResolver) -> T:
        value = resolver.resolve(self)
        if value is None:
            if self.default is None:
                raise ConverterInputError(
                    f"Config field {self.key!r} is required but no "
                    "source provided a value"
                )
            return self.default
        return self._convert(value)

    def read_opt(self, resolver: ChainedConfigResolver) -> T | None:
        value = resolver.resolve(self)
        if value is None:
            return self.default
        return self._convert(value)

    def _convert(self, value: object) -> T:
        try:
            return self.converter.convert(value)
        except ConverterInputError as exc:
            raise ConverterInputError(f"Config field {self.key!r}: {exc}") from exc


class ConfigSource(ABC):
    """Источник сырых значений по :class:`FieldSpec`.

    ``None`` = «пропусти меня», не «поле отсутствует» — отсутствие
    выявляет :meth:`FieldSpec.read` после опроса всех источников.
    """

    @abstractmethod
    def resolve(self, spec: FieldSpec[Any]) -> object | None: ...


class ChainedConfigResolver:
    """Опрашивает источники по порядку; первый non-``None`` выигрывает."""

    def __init__(self, sources: Sequence[ConfigSource]) -> None:
        self._sources = list(sources)

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        for source in self._sources:
            value = source.resolve(spec)
            if value is not None:
                return value
        return None


class StrConverter(Converter[object, str]):
    def convert(self, value: object) -> str:
        if isinstance(value, str):
            return value
        return str(value)


class IntConverter(Converter[object, int]):
    def convert(self, value: object) -> int:
        # bool — подкласс int в Python; для конфига это почти всегда ошибка.
        if isinstance(value, bool):
            raise ConverterInputError(f"expected int, got bool {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError as exc:
                raise ConverterInputError(f"not a valid int: {value!r}") from exc
        raise ConverterInputError(f"cannot convert {type(value).__name__} to int")


class FloatConverter(Converter[object, float]):
    def convert(self, value: object) -> float:
        if isinstance(value, bool):
            raise ConverterInputError(f"expected float, got bool {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError as exc:
                raise ConverterInputError(f"not a valid float: {value!r}") from exc
        raise ConverterInputError(f"cannot convert {type(value).__name__} to float")


class BoolConverter(Converter[object, bool]):
    _TRUE = frozenset({"true", "1", "yes", "on"})
    _FALSE = frozenset({"false", "0", "no", "off"})

    def convert(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in self._TRUE:
                return True
            if normalized in self._FALSE:
                return False
            raise ConverterInputError(f"not a valid bool: {value!r}")
        raise ConverterInputError(f"cannot convert {type(value).__name__} to bool")


class CsvListConverter(Converter[object, list[str]]):
    """``list`` из TOML — как есть; ``str`` из env — split по ``,``."""

    def convert(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item is not None and str(item) != ""]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        raise ConverterInputError(f"cannot convert {type(value).__name__} to list[str]")


class MappingMappingConverter(
    Converter[object, Mapping[str, Mapping[str, str]]]
):
    """Двухуровневая namespaced-карта ``{namespace: {key: value}}``.

    Внешний и внутренний ключи обязаны быть строками; значения
    приводятся к ``str`` (env-источники возвращают только строки,
    TOML — типизированные значения, единый строковый contract упрощает
    потребителям парсинг).
    """

    def convert(self, value: object) -> Mapping[str, Mapping[str, str]]:
        if not isinstance(value, Mapping):
            raise ConverterInputError(
                f"expected Mapping, got {type(value).__name__}"
            )
        result: dict[str, Mapping[str, str]] = {}
        for ns, sub in value.items():
            if not isinstance(ns, str):
                raise ConverterInputError(
                    f"namespace key must be str, got {type(ns).__name__}"
                )
            if not isinstance(sub, Mapping):
                raise ConverterInputError(
                    f"value for namespace {ns!r} must be Mapping, "
                    f"got {type(sub).__name__}"
                )
            inner: dict[str, str] = {}
            for k, v in sub.items():
                if not isinstance(k, str):
                    raise ConverterInputError(
                        f"key in namespace {ns!r} must be str, "
                        f"got {type(k).__name__}"
                    )
                inner[k] = str(v)
            result[ns] = inner
        return result
