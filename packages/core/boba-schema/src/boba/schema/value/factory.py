"""PythonValueFactory: оборачивает Python-примитив через цепочку адаптеров.

Порядок адаптеров значим:
  - BoolAdapter ДО IntAdapter (bool — подкласс int);
  - DateTimeAdapter ДО DateAdapter (datetime — подкласс date).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from boba.patterns import ConverterInputError
from boba.schema.value.base import (
    PythonValueAdapter,
    ScalarValue,
)

__all__ = ["PythonValueFactory"]


class PythonValueFactory:
    """Фабрика ConfigValue: первый согласившийся адаптер выигрывает."""

    def __init__(self, adapters: Sequence[PythonValueAdapter]) -> None:
        self._adapters = tuple(adapters)

    def from_python(self, value: Any) -> ScalarValue:
        for adapter in self._adapters:
            wrapped = adapter.try_wrap(value)
            if wrapped is not None:
                return wrapped

        raise ConverterInputError(f"cannot wrap {type(value).__name__} as ConfigValue")
