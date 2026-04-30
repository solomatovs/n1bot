"""PythonValueFactory: оборачивает Python-примитив через цепочку адаптеров.

Порядок адаптеров значим:
  - BoolAdapter ДО IntAdapter (bool — подкласс int);
  - DateTimeAdapter ДО DateAdapter (datetime — подкласс date).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from boba.domain.core.confignext.value.base import (
    ConfigValue,
    PythonValueAdapter,
)
from boba.domain.core.patterns import ConverterInputError

__all__ = ["PythonValueFactory"]


class PythonValueFactory:
    """Фабрика ConfigValue: первый согласившийся адаптер выигрывает."""

    def __init__(self, adapters: Sequence[PythonValueAdapter]) -> None:
        self._adapters = tuple(adapters)

    def from_python(self, value: Any) -> ConfigValue:
        for adapter in self._adapters:
            wrapped = adapter.try_wrap(value)
            if wrapped is not None:
                return wrapped

        raise ConverterInputError(f"cannot wrap {type(value).__name__} as ConfigValue")
