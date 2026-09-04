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
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field

from boba.catalog.base import CatalogError, CatalogInvariantError
from boba.catalog.sources import (
    Keyed,
    ManualColumn,
    ManualObject,
    ManualObjectKind,
    NodeColumn,
    ObjectCard,
    ObjectFamily,
    ObjectKind,
    ObjectRef,
    PartKind,
    Records,
    SnapshotPart,
    SourceObject,
    SourceRecord,
    SourceSnapshot,
    SubPart,
    TreeKind,
    TreeNode,
)

__all__ = [
    "ChCardKind",
    "ChColumn",
    "ChDatabase",
    "ChDictionary",
    "ChDictionaryAttribute",
    "ChDictionaryCard",
    "ChGroup",
    "ChPart",
    "ChSnapshot",
    "ChSourceKind",
    "ChTable",
    "ChTableCard",
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

    KEY: ClassVar[tuple[str, ...]] = ("name",)
    PARENT: ClassVar[tuple[str, ...]] = ()


class ChTable(SourceObject):
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

    KEY: ClassVar[tuple[str, ...]] = ("database", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database",)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.TABLE

    @property
    def label(self) -> str:
        return self.name

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        return ChTableCard(
            ref=ref,
            table=self,
            columns=snapshot.parts_of_type(ref, PartKind.COLUMN, ChColumn),
        )


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

    KEY: ClassVar[tuple[str, ...]] = ("database", "table", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "table")
    ORDER: ClassVar[tuple[str, ...]] = ("position",)


class ChDictionary(SourceObject):
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

    KEY: ClassVar[tuple[str, ...]] = ("database", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database",)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.DICTIONARY

    @property
    def label(self) -> str:
        return self.name

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        return ChDictionaryCard(
            ref=ref,
            dictionary=self,
            attributes=snapshot.parts_of_type(
                ref, PartKind.ATTRIBUTE, ChDictionaryAttribute
            ),
        )


class ChDictionaryAttribute(SourceRecord):
    database: str = Field(min_length=1)
    dictionary: str = Field(min_length=1)
    name: str = Field(min_length=1)
    position: int = Field(ge=1)
    type: str = Field(min_length=1)

    KEY: ClassVar[tuple[str, ...]] = ("database", "dictionary", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "dictionary")
    ORDER: ClassVar[tuple[str, ...]] = ("position",)


class ChPart(StrEnum):
    """Части снимка ClickHouse: имена полей ChSnapshot."""

    DATABASES = "databases"
    TABLES = "tables"
    COLUMNS = "columns"
    DICTIONARIES = "dictionaries"
    DICTIONARY_ATTRIBUTES = "dictionary_attributes"


class ChSourceKind(StrEnum):
    """kind типа соединения, которым владеет этот пакет; тем же именем снимок
    зарегистрирован в группе boba.catalog."""

    CLICKHOUSE = "clickhouse"


class ChCardKind(StrEnum):
    """Дискриминаторы карточек объектов этого вида источника."""

    CH_TABLE = "ch_table"
    CH_DICTIONARY = "ch_dictionary"


class ChTableCard(ObjectCard):
    card: Literal[ChCardKind.CH_TABLE] = ChCardKind.CH_TABLE
    table: ChTable
    columns: tuple[ChColumn, ...]


class ChDictionaryCard(ObjectCard):
    card: Literal[ChCardKind.CH_DICTIONARY] = ChCardKind.CH_DICTIONARY
    dictionary: ChDictionary
    attributes: tuple[ChDictionaryAttribute, ...]


class ChPathDepth(IntEnum):
    """Длина пути объекта ручного источника: база, имя."""

    OBJECT = 2


class ChNullable:
    """Nullable-обёртка типов ClickHouse: тип колонки говорит о nullable сам."""

    PREFIX: ClassVar[str] = "Nullable("

    @classmethod
    def wraps(cls, type_name: str) -> bool:
        return type_name.startswith(cls.PREFIX)


class ChSnapshot(SourceSnapshot):
    """Снимок ClickHouse одной версии: плоские таблицы записей.

    Реализация SourceSnapshot: части и семейства объявлены, инварианты и
    поиск по адресу даёт база; родное здесь — дерево (база → группа →
    объект), карточки, колонки узла по ключам движка и объекты ручного
    источника.
    """

    TABLE_PREFIX: ClassVar[str] = "ch"
    MANUAL_KIND: ClassVar[ObjectKind] = ObjectKind.TABLE
    PARTS: ClassVar[tuple[SnapshotPart, ...]] = (
        SnapshotPart(name=ChPart.DATABASES, model=ChDatabase, label="database"),
        SnapshotPart(
            name=ChPart.TABLES, model=ChTable, label="table", parent=ChPart.DATABASES
        ),
        SnapshotPart(
            name=ChPart.COLUMNS, model=ChColumn, label="column", parent=ChPart.TABLES
        ),
        SnapshotPart(
            name=ChPart.DICTIONARIES,
            model=ChDictionary,
            label="dictionary",
            parent=ChPart.DATABASES,
        ),
        SnapshotPart(
            name=ChPart.DICTIONARY_ATTRIBUTES,
            model=ChDictionaryAttribute,
            label="dictionary attribute",
            parent=ChPart.DICTIONARIES,
        ),
    )
    FAMILIES: ClassVar[tuple[ObjectFamily, ...]] = (
        ObjectFamily(
            kind=ObjectKind.TABLE,
            part=ChPart.TABLES,
            subparts=(SubPart(kind=PartKind.COLUMN, part=ChPart.COLUMNS),),
        ),
        ObjectFamily(
            kind=ObjectKind.DICTIONARY,
            part=ChPart.DICTIONARIES,
            subparts=(
                SubPart(kind=PartKind.ATTRIBUTE, part=ChPart.DICTIONARY_ATTRIBUTES),
            ),
        ),
    )

    kind: Literal[ChSourceKind.CLICKHOUSE] = ChSourceKind.CLICKHOUSE
    databases: tuple[ChDatabase, ...] = ()
    tables: tuple[ChTable, ...] = ()
    columns: tuple[ChColumn, ...] = ()
    dictionaries: tuple[ChDictionary, ...] = ()
    dictionary_attributes: tuple[ChDictionaryAttribute, ...] = ()

    def node_columns(self, ref: ObjectRef) -> tuple[NodeColumn, ...]:
        if ref.kind is not ObjectKind.TABLE:
            return ()

        columns: list[NodeColumn] = []
        for column in self.columns_of(ref.path):
            columns.append(
                NodeColumn(
                    name=column.name,
                    type=column.type,
                    nullable=ChNullable.wraps(column.type),
                    key=column.in_primary_key,
                )
            )

        return tuple(columns)

    def with_object(self, obj: ManualObject) -> ChSnapshot:
        if len(obj.path) != ChPathDepth.OBJECT:
            msg = f"clickhouse object path must be database/name: {'/'.join(obj.path)}"
            raise CatalogInvariantError([msg])

        database, name = obj.path
        databases = self.databases
        if (database,) not in Keyed.keys_of(self.databases):
            databases = (*databases, ChDatabase(name=database))

        kind = ChTableKind.TABLE
        if obj.kind is ManualObjectKind.VIEW:
            kind = ChTableKind.VIEW

        table = ChTable(database=database, name=name, kind=kind, comment=obj.comment)
        columns = list(self.columns)
        for position, column in enumerate(obj.columns, start=1):
            columns.append(
                ChColumn(
                    database=database,
                    table=name,
                    name=column.name,
                    position=position,
                    type=column.type,
                    comment=column.comment,
                )
            )

        return self.model_copy(
            update={
                "databases": databases,
                "tables": (*self.tables, table),
                "columns": tuple(columns),
            }
        )

    def without_object(self, path: Sequence[str]) -> ChSnapshot:
        wanted = tuple(path)
        return self.model_copy(
            update={
                "tables": Records.without_key(self.tables, wanted),
                "columns": Records.without_parent(self.columns, wanted),
            }
        )

    def manual_object(self, ref: ObjectRef) -> ManualObject:
        found = self.require_object(ref)
        if not isinstance(found, ChTable):
            msg = f"{ref.kind.value} at {ref.render()} is not a manual object"
            raise CatalogError(msg)

        kind = ManualObjectKind.TABLE
        if found.kind is ChTableKind.VIEW:
            kind = ManualObjectKind.VIEW

        columns: list[ManualColumn] = []
        for column in self.columns_of(ref.path):
            columns.append(
                ManualColumn(
                    name=column.name,
                    type=column.type,
                    nullable=ChNullable.wraps(column.type),
                    comment=column.comment,
                )
            )

        return ManualObject(
            kind=kind, path=found.key, comment=found.comment, columns=tuple(columns)
        )

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
