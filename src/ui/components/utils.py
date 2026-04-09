"""Утилиты для UI-компонентов."""
from __future__ import annotations

from typing import Sequence, TypeVar

T = TypeVar("T")


def safe_index(items: Sequence[T], value: T, default: int = 0) -> int:
    """Найти индекс элемента в последовательности, или вернуть default."""
    try:
        return list(items).index(value)
    except ValueError:
        return default
