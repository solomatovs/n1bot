"""Снимок источника ClickHouse в родной структуре: базы, таблицы с движком,
ключами и настройками, представления, материализованные представления,
словари с атрибутами. Поля названы как в system.tables, system.columns,
system.dictionaries.

Ошибки:
CatalogInvariantError — повторы ключей или запись без родителя.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import IntEnum, StrEnum
from operator import attrgetter
from typing import Literal
from uuid import UUID

from pydantic import Field

from boba.catalog.model import CatalogModel
from boba.catalog.sources import (
    Keyed,
    ObjectKind,
    ObjectRef,
    SourceKind,
    SourceRecord,
    TreeKind,
    TreeNode,
)

__all__ = [
    "ChColumn",
    "ChDatabase",
    "ChDictionary",
    "ChDictionaryAttribute",
    "ChGroup",
    "ChSnapshot",
    "ChTable",
    "ChTableKind",
]


class ChTableKind(StrEnum):
    TABLE = "table"
    VIEW = "view"
    MATERIALIZED = "materialized"
    LIVE = "live"
    DICTIONARY_TABLE = "dictionary_table"


class ChDepth(IntEnum):
    """Глубина пути в дереве ClickHouse."""

    DATABASES = 0
    GROUPS = 1
    OBJECTS = 2


class ChGroup(StrEnum):
    """Группы объектов под базой в дереве; значение — ступень пути."""

    TABLES = "tables"
    VIEWS = "views"
    MATERIALIZED = "materialized"
    DICTIONARIES = "dictionaries"

    @classmethod
    def of_table(cls, kind: ChTableKind) -> ChGroup:
        if kind is ChTableKind.VIEW:
            return cls.VIEWS

        if kind is ChTableKind.LIVE:
            return cls.VIEWS

        if kind is ChTableKind.MATERIALIZED:
            return cls.MATERIALIZED

        return cls.TABLES


class ChDatabase(SourceRecord):
    name: str = Field(min_length=1)
    engine: str = ""
    comment: str | None = None

    @property
    def key(self) -> tuple[str, ...]:
        return (self.name,)

    @property
    def parent(self) -> tuple[str, ...]:
        return ()


class ChTable(SourceRecord):
    """Таблица, представление или материализованное представление."""

    database: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: ChTableKind
    engine: str = ""
    engine_full: str = ""
    comment: str | None = None
    partition_key: str | None = None
    sorting_key: str | None = None
    primary_key: str | None = None
    sampling_key: str | None = None
    ttl: str | None = None
    settings: dict[str, str] = Field(default_factory=dict)
    definition: str | None = None
    target: str | None = None
    dependencies: tuple[str, ...] = ()
    total_rows: int | None = None
    total_bytes: int | None = None
    metadata_modified_at: str = ""
    create_query: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database,)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.TABLE

    @property
    def label(self) -> str:
        return self.name


class ChColumn(SourceRecord):
    database: str = Field(min_length=1)
    table: str = Field(min_length=1)
    name: str = Field(min_length=1)
    position: int = Field(ge=1)
    type: str = Field(min_length=1)
    default_kind: str | None = None
    default_expression: str | None = None
    comment: str | None = None
    codec: str | None = None
    ttl: str | None = None
    in_partition_key: bool = False
    in_sorting_key: bool = False
    in_primary_key: bool = False
    in_sampling_key: bool = False

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.table, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.table)


class ChDictionary(SourceRecord):
    database: str = Field(min_length=1)
    name: str = Field(min_length=1)
    status: str = ""
    layout: str = ""
    source: str = ""
    key_columns: tuple[str, ...] = ()
    lifetime_min: int = 0
    lifetime_max: int = 0
    comment: str | None = None
    create_query: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database,)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.DICTIONARY

    @property
    def label(self) -> str:
        return self.name


class ChDictionaryAttribute(SourceRecord):
    database: str = Field(min_length=1)
    dictionary: str = Field(min_length=1)
    name: str = Field(min_length=1)
    position: int = Field(ge=1)
    type: str = Field(min_length=1)

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.dictionary, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.dictionary)


class ChSnapshot(CatalogModel):
    """Снимок ClickHouse одной версии: плоские таблицы записей. Дерево:
    база → группа → объект."""

    kind: Literal[SourceKind.CLICKHOUSE] = SourceKind.CLICKHOUSE
    databases: tuple[ChDatabase, ...] = ()
    tables: tuple[ChTable, ...] = ()
    columns: tuple[ChColumn, ...] = ()
    dictionaries: tuple[ChDictionary, ...] = ()
    dictionary_attributes: tuple[ChDictionaryAttribute, ...] = ()

    @classmethod
    def empty(cls) -> ChSnapshot:
        return cls()

    def check(self) -> None:
        """Ключи уникальны, у каждой записи есть родитель.

        Ошибки:
        CatalogInvariantError — с перечнем нарушений.
        """
        Keyed.require_unique("database", self.databases)
        Keyed.require_unique("table", self.tables)
        Keyed.require_unique("column", self.columns)
        Keyed.require_unique("dictionary", self.dictionaries)
        Keyed.require_unique("dictionary attribute", self.dictionary_attributes)

        databases = Keyed.keys_of(self.databases)
        tables = Keyed.keys_of(self.tables)
        dictionaries = Keyed.keys_of(self.dictionaries)
        Keyed.require_parents("table", self.tables, databases)
        Keyed.require_parents("column", self.columns, tables)
        Keyed.require_parents("dictionary", self.dictionaries, databases)
        Keyed.require_parents(
            "dictionary attribute", self.dictionary_attributes, dictionaries
        )

    def objects_count(self) -> int:
        return len(self.tables) + len(self.dictionaries)

    def table(self, path: Sequence[str]) -> ChTable | None:
        for table in self.tables:
            if table.key == tuple(path):
                return table

        return None

    def dictionary(self, path: Sequence[str]) -> ChDictionary | None:
        for dictionary in self.dictionaries:
            if dictionary.key == tuple(path):
                return dictionary

        return None

    def columns_of(self, path: Sequence[str]) -> Iterator[ChColumn]:
        wanted = tuple(path)
        for column in sorted(self.columns, key=attrgetter("position")):
            if column.parent != wanted:
                continue

            yield column

    def attributes_of(self, path: Sequence[str]) -> Iterator[ChDictionaryAttribute]:
        wanted = tuple(path)
        for attribute in sorted(self.dictionary_attributes, key=attrgetter("position")):
            if attribute.parent != wanted:
                continue

            yield attribute

    def children(self, source_id: UUID, path: Sequence[str]) -> Sequence[TreeNode]:
        """Дети узла дерева по глубине пути: базы, группы, объекты."""
        steps = tuple(path)
        depth = len(steps)
        if depth == ChDepth.DATABASES:
            return list(self._database_nodes())

        if depth == ChDepth.GROUPS:
            return list(self._group_nodes(steps))

        if depth == ChDepth.OBJECTS:
            return list(self._object_nodes(source_id, steps))

        return []

    def _database_nodes(self) -> Iterator[TreeNode]:
        for database in sorted(self.databases, key=attrgetter("name")):
            groups = list(self._group_nodes(database.key))
            yield TreeNode(
                path=database.key,
                label=database.name,
                kind=TreeKind.DATABASE,
                children_count=len(groups),
                detail=database.engine,
                comment=database.comment,
            )

    def _group_counts(self, steps: tuple[str, ...]) -> dict[ChGroup, int]:
        counts: dict[ChGroup, int] = {}
        for table in self.tables:
            if table.parent != steps:
                continue

            group = ChGroup.of_table(table.kind)
            counts[group] = counts.get(group, 0) + 1

        for dictionary in self.dictionaries:
            if dictionary.parent != steps:
                continue

            counts[ChGroup.DICTIONARIES] = counts.get(ChGroup.DICTIONARIES, 0) + 1

        return counts

    def _group_nodes(self, steps: tuple[str, ...]) -> Iterator[TreeNode]:
        counts = self._group_counts(steps)
        for group in ChGroup:
            if group not in counts:
                continue

            yield TreeNode(
                path=(*steps, group.value),
                label=group.value,
                kind=TreeKind.GROUP,
                children_count=counts[group],
            )

    def _object_nodes(
        self, source_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, group = steps
        if group == ChGroup.DICTIONARIES.value:
            yield from self._dictionary_nodes(source_id, steps)
            return

        for table in sorted(self.tables, key=attrgetter("name")):
            if table.database != database:
                continue

            if ChGroup.of_table(table.kind).value != group:
                continue

            yield TreeNode(
                path=(*steps, table.name),
                label=table.name,
                kind=TreeKind.OBJECT,
                children_count=0,
                detail=table.engine,
                comment=table.comment,
                ref=ObjectRef(
                    source_id=source_id, kind=ObjectKind.TABLE, path=table.key
                ),
            )

    def _dictionary_nodes(
        self, source_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, _group = steps
        for dictionary in sorted(self.dictionaries, key=attrgetter("name")):
            if dictionary.database != database:
                continue

            yield TreeNode(
                path=(*steps, dictionary.name),
                label=dictionary.name,
                kind=TreeKind.OBJECT,
                children_count=0,
                detail=dictionary.layout,
                comment=dictionary.comment,
                ref=ObjectRef(
                    source_id=source_id,
                    kind=ObjectKind.DICTIONARY,
                    path=dictionary.key,
                ),
            )
