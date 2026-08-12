"""Filter — backend-agnostic DSL предикатов для запросов по полям чанка."""

from __future__ import annotations

from abc import ABC
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "And",
    "Eq",
    "Filter",
    "Gt",
    "Gte",
    "HasAllTags",
    "HasAnyTag",
    "HasTag",
    "In",
    "Lt",
    "Lte",
    "Ne",
    "Not",
    "NotIn",
    "Or",
    "ScalarValue",
    "UnsupportedFilterError",
]

ScalarValue = str | int | float | bool
"""Допустимые типы значений metadata в Filter (chroma-совместимо)."""


class Filter(ABC):  # noqa: B024 — sealed-маркер, методы не нужны
    """Sealed: ниже все валидные подклассы. Backend-impl делает pattern-match."""


@dataclass(frozen=True)
class Eq(Filter):
    """field == value"""

    field: str
    value: ScalarValue


@dataclass(frozen=True)
class Ne(Filter):
    """field != value"""

    field: str
    value: ScalarValue


@dataclass(frozen=True)
class Lt(Filter):
    """field < value (числовое сравнение)."""

    field: str
    value: int | float


@dataclass(frozen=True)
class Lte(Filter):
    """field <= value (числовое сравнение)."""

    field: str
    value: int | float


@dataclass(frozen=True)
class Gt(Filter):
    """field > value (числовое сравнение)."""

    field: str
    value: int | float


@dataclass(frozen=True)
class Gte(Filter):
    """field >= value (числовое сравнение)."""

    field: str
    value: int | float


@dataclass(frozen=True)
class In(Filter):
    """field IN values — ровный список допустимых значений."""

    field: str
    values: Sequence[ScalarValue]


@dataclass(frozen=True)
class NotIn(Filter):
    """field NOT IN values"""

    field: str
    values: Sequence[ScalarValue]


@dataclass(frozen=True)
class HasTag(Filter):
    """Чанк имеет тэг tag в множестве своих тэгов."""

    tag: str


@dataclass(frozen=True)
class HasAnyTag(Filter):
    """Чанк имеет хотя бы один из перечисленных тэгов."""

    tags: Sequence[str]


@dataclass(frozen=True)
class HasAllTags(Filter):
    """Чанк имеет все перечисленные тэги одновременно."""

    tags: Sequence[str]


@dataclass(frozen=True)
class And(Filter):
    """Логическое AND над списком подфильтров."""

    filters: Sequence[Filter]


@dataclass(frozen=True)
class Or(Filter):
    """Логическое OR над списком подфильтров."""

    filters: Sequence[Filter]


@dataclass(frozen=True)
class Not(Filter):
    """Логическое NOT над подфильтром."""

    filter: Filter


class UnsupportedFilterError(Exception):
    """Backend не поддерживает данный предикат / комбинацию."""

    def __init__(self, predicate: Filter, backend: str, reason: str) -> None:
        super().__init__(
            f"backend {backend!r} cannot translate filter "
            f"{type(predicate).__name__}: {reason}"
        )
        self.predicate = predicate
        self.backend = backend
        self.reason = reason
