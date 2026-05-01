"""Базовая инфраструктура конвертеров: MISSING, Pass, ChainConverter, ValueConverter."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar, Final, Generic, TypeVar

from boba_next.value import (
    BoolAdapter,
    ConfigValue,
    DateAdapter,
    DateTimeAdapter,
    FloatAdapter,
    IntAdapter,
    NullAdapter,
    PythonValueFactory,
    StringAdapter,
    TimeAdapter,
)
from boba_next.patterns import Converter

__all__ = [
    "MISSING",
    "ChainConverter",
    "Pass",
    "ValueConverter",
]


T = TypeVar("T")


class _MissingType:
    """Singleton-sentinel «значения не было»."""

    _instance: ClassVar[_MissingType | None] = None

    def __new__(cls) -> _MissingType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _MissingType()


class Pass(Converter[Any, Any]):
    """No-op конвертер."""

    def convert(self, value: Any) -> Any:
        return value


class ChainConverter(Converter[Any, T], Generic[T]):
    """Последовательно применяет конвертеры."""

    def __init__(self, *converters: Converter[Any, Any]) -> None:
        self._converters = converters

    def convert(self, value: Any) -> T:
        for c in self._converters:
            value = c.convert(value)

        return value


class ValueConverter(Converter[Any, Any]):
    """База тип-конвертеров; MISSING пропускается; вход → ConfigValue.

    Содержит две пограничных утилиты:
      - `unwrap(value)` — MISSING/ConfigValue/primitive → primitive (или MISSING).
      - `ensure(value)` — primitive (после Default) оборачивается через
        свежую PythonValueFactory; ConfigValue остаётся как есть.
    Используются и подклассами, и Required/Nullable снаружи.
    """

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            return MISSING

        return self._convert_value(self.ensure(value))

    @abstractmethod
    def _convert_value(self, value: ConfigValue) -> Any: ...

    @classmethod
    def ensure(cls, value: Any) -> ConfigValue:
        """ConfigValue остаётся; primitive (после Default) оборачивается."""
        if isinstance(value, ConfigValue):
            return value

        return PythonValueFactory(
            (
                StringAdapter(),
                BoolAdapter(),
                IntAdapter(),
                FloatAdapter(),
                NullAdapter(),
                DateTimeAdapter(),
                DateAdapter(),
                TimeAdapter(),
            )
        ).from_python(value)

    @classmethod
    def unwrap(cls, value: Any) -> Any:
        """MISSING → MISSING; ConfigValue → primitive; primitive → as is."""
        if value is MISSING:
            return MISSING

        if isinstance(value, ConfigValue):
            return value.unwrap()

        return value
