"""Базовая инфраструктура coercer-цепочки: Coercer ABC + MISSING + Pass +
ChainCoercer + ValueCoercer + SchemaContributor (mixin для wire-проекции).

Coercer — узкая абстракция «привести raw-значение к типизированному +
валидировать». В отличие от общего `Converter[A, B]` (boba.patterns),
Coercer применяется только в pipeline'ах `FieldSpec.coercer` /
`ScalarItem.coercer` / `ObjectSchema.invariants` и имеет метод `apply()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Final, Generic, TypeVar

from boba.value import (
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

__all__ = [
    "MISSING",
    "ChainCoercer",
    "Coercer",
    "Pass",
    "SchemaContributor",
    "ValueCoercer",
]


A = TypeVar("A")
B = TypeVar("B")
T = TypeVar("T")


class Coercer(ABC, Generic[A, B]):
    """Базовый интерфейс «coercer»: привести `value: A` к `B` с валидацией.

    `apply(value)` — единственная точка входа. Может бросать `ConverterInputError`
    (или подкласс) при невалидном входе. Подклассы могут добавлять mixin
    `SchemaContributor` для проекции в JSON-Schema.
    """

    @abstractmethod
    def apply(self, value: A) -> B: ...


class SchemaContributor(ABC):
    """Mixin для coercer'ов, умеющих сообщать свою грань JSON-Schema."""

    @abstractmethod
    def contribute(self, prop: dict[str, Any]) -> None:
        """Дополнить JSON-Schema fragment этого coercer'а."""
        ...


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


class Pass(Coercer[Any, Any]):
    """No-op coercer."""

    def apply(self, value: Any) -> Any:
        return value


class ChainCoercer(Coercer[Any, T], SchemaContributor, Generic[T]):
    """Последовательно применяет coercer'ы; SchemaContributor агрегирует fragments."""

    def __init__(self, *coercers: Coercer[Any, Any]) -> None:
        self._coercers = coercers

    def apply(self, value: Any) -> T:
        for c in self._coercers:
            value = c.apply(value)

        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        """Каждое звено-SchemaContributor дополняет общий fragment."""
        for c in self._coercers:
            if isinstance(c, SchemaContributor):
                c.contribute(prop)


class ValueCoercer(Coercer[Any, Any]):
    """База тип-coercer'ов; MISSING пропускается; вход → ConfigValue.

    Содержит две пограничные утилиты:
      - `unwrap(value)` — MISSING/ConfigValue/primitive → primitive (или MISSING).
      - `ensure(value)` — primitive (после Default) оборачивается через
        свежую PythonValueFactory; ConfigValue остаётся как есть.
    Используются и подклассами, и NotNull/Nullable снаружи.
    """

    def apply(self, value: Any) -> Any:
        if value is MISSING:
            return MISSING

        return self._apply_value(self.ensure(value))

    @abstractmethod
    def _apply_value(self, value: ConfigValue) -> Any: ...

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
