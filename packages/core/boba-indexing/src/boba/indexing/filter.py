"""Filter — backend-agnostic DSL для запросов по полям чанка
(tags, source_id, updated_at, scope-key impl'а вроде namespace,
произвольная business-Metadata — что угодно из payload'а чанка).

Sealed union предикатов. Каждый ChunkStore-impl переводит Filter в свой
нативный язык запросов:
- ChromaVectorStore  -> chroma where={...} syntax
- PostgresChunkStore -> SQL WHERE clause
- QdrantVectorStore  -> qdrant Filter struct
- ...

Использование:

    from boba.indexing.filter import And, Eq, HasTag, Lt
    from boba.indexing.index_views import TrackingKeys

    # stale-чанки касаемых источников за время до cutoff:
    f = And([
        Eq(TrackingKeys.NAMESPACE, "docs"),
        Lt(TrackingKeys.UPDATED_AT, 1700000000.0),
        In(TrackingKeys.SOURCE_ID, ["src/page-1", "src/page-2"]),
    ])

    # чанки с тегом "internal":
    f = HasTag("internal")

    # композиция:
    f = And([HasTag("public"), Or([Eq("doc_type", "pdf"), Eq("doc_type", "html")])])

DSL покрывает реалистичный набор операций vector-backend'ов; экзотику
конкретный backend может либо отвергать через UnsupportedFilterError,
либо реализовывать через комбинацию своих примитивов.
"""

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


class Filter(ABC):
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
            f"backend {backend!r} cannot translate filter {type(predicate).__name__}: {reason}"
        )
        self.predicate = predicate
        self.backend = backend
        self.reason = reason
