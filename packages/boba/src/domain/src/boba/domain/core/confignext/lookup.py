"""ConfigLookup: явная Found/NotFound семантика для отсутствующих значений."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

__all__ = [
    "ConfigLookup",
    "Found",
    "NotFound",
]


T = TypeVar("T")
U = TypeVar("U")


class ConfigLookup(ABC, Generic[T]):
    """Результат поиска: Found(value) или NotFound."""

    @abstractmethod
    def is_found(self) -> bool: ...

    @abstractmethod
    def value(self) -> T:
        """Достать значение; raises LookupError если NotFound."""
        ...

    @abstractmethod
    def or_else(self, default: T) -> T:
        """Значение либо default."""
        ...


@dataclass(frozen=True)
class Found(ConfigLookup[T], Generic[T]):
    """Значение найдено (в т.ч. может быть NullValue)."""

    found: T

    def is_found(self) -> bool:
        return True

    def value(self) -> T:
        return self.found

    def or_else(self, default: T) -> T:
        return self.found


class NotFound(ConfigLookup[T], Generic[T]):
    """Значение отсутствует. Используй sentinel NOT_FOUND, а не создавай напрямую."""

    __slots__ = ()

    def is_found(self) -> bool:
        return False

    def value(self) -> T:
        raise LookupError("value not found")

    def or_else(self, default: T) -> T:
        return default

    def __repr__(self) -> str:
        return "NotFound()"
