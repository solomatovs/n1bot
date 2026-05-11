"""Базовые ABC: ConfigValue + PythonValueAdapter.

Каждый файл рядом — один тип значения вместе со своим адаптером
(`string_value.py`, `int_value.py`, …). Фабрика — в `factory.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from boba.patterns import ConverterInputError

__all__ = ["PythonValueAdapter", "ScalarValue"]


class ScalarValue(ABC):
    """Типизированное скалярное значение конфига; знает свои конверсии.

    `unwrap()` — возвращает соответствующий Python-примитив.
    `as_int / as_float / as_bool / as_string` — полиморфные конверсии:
      по умолчанию бросают ConverterInputError; подкласс переопределяет
      только те, которые умеет (без обратного coercion).
    """

    @abstractmethod
    def unwrap(self) -> Any:
        """Вернуть соответствующий Python-примитив."""

    def as_int(self) -> int:
        raise ConverterInputError(f"expected int, got {self._kind()}")

    def as_float(self) -> float:
        raise ConverterInputError(f"expected float, got {self._kind()}")

    def as_bool(self) -> bool:
        raise ConverterInputError(f"expected bool, got {self._kind()}")

    def as_string(self) -> str:
        raise ConverterInputError(f"expected string, got {self._kind()}")

    def _kind(self) -> str:
        """Короткое имя для error-сообщений: «int», «string», «null», ..."""
        return type(self).__name__.removesuffix("Value").lower()


class PythonValueAdapter(ABC):
    """Адаптер Python-примитива в один из ConfigValue."""

    @abstractmethod
    def try_wrap(self, value: Any) -> ScalarValue | None:
        """Вернуть ConfigValue, если значение подходит этому адаптеру; иначе None."""
