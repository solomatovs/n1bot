"""Снимок источника ClickHouse в родной структуре: базы, таблицы с движком,
ключами и настройками, представления, материализованные представления,
словари с атрибутами. Поля названы как в system.tables, system.columns,
system.dictionaries.

Ошибки:
CatalogInvariantError — повторы ключей или запись без родителя.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from enum import IntEnum, StrEnum
from operator import attrgetter
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field

from boba.catalog.base import CatalogModel
from boba.catalog.sources import (
    NodeColumn,
    ObjectCard,
    ObjectFamily,
    ObjectKind,
    ObjectRef,
    PartKind,
    PartScope,
    SnapshotPart,
    SourceObject,
    SourceRecord,
    SourceSnapshot,
    SubPart,
    TreeKind,
    TreeNode,
    TreeScope,
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
    "ChTypeFamily",
    "ChTypeName",
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

    def part(self) -> str:
        """Часть снимка, в которой лежат объекты группы."""
        if self is ChGroup.DICTIONARIES:
            return ChPart.DICTIONARIES

        return ChPart.TABLES


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
    VOLATILE: ClassVar[frozenset[str]] = frozenset(
        {"total_rows", "total_bytes", "metadata_modified_at"}
    )

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
    VOLATILE: ClassVar[frozenset[str]] = frozenset({"status"})
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


class ChNullable:
    """Nullable-обёртка типов ClickHouse: тип колонки говорит о nullable сам."""

    PREFIX: ClassVar[str] = "Nullable("

    @classmethod
    def wraps(cls, type_name: str) -> bool:
        return type_name.startswith(cls.PREFIX)


class ChTypeFamily(StrEnum):
    """Семейства типов ClickHouse, внутри которых тип бывает шире другого."""

    STRING = "string"
    FIXED_STRING = "fixed_string"
    INT = "int"
    UINT = "uint"
    FLOAT = "float"
    DECIMAL = "decimal"
    OTHER = "other"


class ChTypeName(CatalogModel):
    """Тип колонки ClickHouse: семейство, разрядность, длина, точность и
    масштаб, признак Nullable; LowCardinality прозрачна. Знает, шире ли один
    тип другого; незнакомые типы сравниваются как строки."""

    WRAPPERS: ClassVar[tuple[str, ...]] = ("LowCardinality",)
    NULLABLE: ClassVar[str] = "Nullable"
    INT_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^(U?)Int(8|16|32|64|128|256)$"
    )
    FLOAT_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"^Float(32|64)$")
    FIXED_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"^FixedString\((\d+)\)$")
    DECIMAL_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^Decimal(?:(32|64|128|256)\((\d+)\)|\((\d+),\s*(\d+)\))$"
    )
    DECIMAL_PRECISION: ClassVar[Mapping[str, int]] = {
        "32": 9,
        "64": 18,
        "128": 38,
        "256": 76,
    }
    UNBOUNDED: ClassVar[int] = 10**9

    raw: str
    family: ChTypeFamily
    nullable: bool = False
    bits: int = 0
    length: int = UNBOUNDED
    precision: int = 0
    scale: int = 0

    @classmethod
    def parse(cls, raw: str) -> ChTypeName:
        text = raw.strip()
        inner, nullable = cls._unwrap(text)

        if inner == "String":
            return cls(raw=text, family=ChTypeFamily.STRING, nullable=nullable)

        fixed = cls.FIXED_PATTERN.match(inner)
        if fixed is not None:
            return cls(
                raw=text,
                family=ChTypeFamily.FIXED_STRING,
                nullable=nullable,
                length=int(fixed.group(1)),
            )

        integer = cls.INT_PATTERN.match(inner)
        if integer is not None:
            family = ChTypeFamily.INT
            if integer.group(1) == "U":
                family = ChTypeFamily.UINT

            return cls(
                raw=text, family=family, nullable=nullable, bits=int(integer.group(2))
            )

        floating = cls.FLOAT_PATTERN.match(inner)
        if floating is not None:
            return cls(
                raw=text,
                family=ChTypeFamily.FLOAT,
                nullable=nullable,
                bits=int(floating.group(1)),
            )

        decimal = cls.DECIMAL_PATTERN.match(inner)
        if decimal is not None:
            return cls._decimal(text, nullable, decimal)

        return cls(raw=text, family=ChTypeFamily.OTHER, nullable=nullable)

    @classmethod
    def _unwrap(cls, text: str) -> tuple[str, bool]:
        """Тип без обёрток LowCardinality и Nullable; был ли Nullable."""
        inner = text
        nullable = False
        while True:
            wrapper = cls._wrapper_of(inner)
            if wrapper is None:
                return inner, nullable

            inner = inner[len(wrapper) + 1 : -1].strip()
            if wrapper == cls.NULLABLE:
                nullable = True

    @classmethod
    def _wrapper_of(cls, text: str) -> str | None:
        for wrapper in (*cls.WRAPPERS, cls.NULLABLE):
            if text.startswith(f"{wrapper}(") and text.endswith(")"):
                return wrapper

        return None

    @classmethod
    def _decimal(cls, text: str, nullable: bool, match: re.Match[str]) -> ChTypeName:
        if match.group(1) is not None:
            precision = cls.DECIMAL_PRECISION[match.group(1)]
            scale = int(match.group(2))
        else:
            precision = int(match.group(3))
            scale = int(match.group(4))

        return cls(
            raw=text,
            family=ChTypeFamily.DECIMAL,
            nullable=nullable,
            precision=precision,
            scale=scale,
        )

    def accepts(self, old: ChTypeName) -> bool:
        """Этот тип принимает всё, что принимал old: равен ему или шире;
        Nullable не сужается."""
        if self.raw == old.raw:
            return True

        if old.nullable and not self.nullable:
            return False

        if self.family is old.family:
            return self._wider_in_family(old)

        return self._wider_across(old)

    def _wider_across(self, old: ChTypeName) -> bool:
        """Расширение в другое семейство: FixedString → String, UInt → Int
        большей разрядности."""
        fixed_to_string = (
            old.family is ChTypeFamily.FIXED_STRING
            and self.family is ChTypeFamily.STRING
        )
        if fixed_to_string:
            return True

        unsigned_to_signed = (
            old.family is ChTypeFamily.UINT and self.family is ChTypeFamily.INT
        )
        if unsigned_to_signed:
            return self.bits > old.bits

        return False

    def _wider_in_family(self, old: ChTypeName) -> bool:
        if self.family is ChTypeFamily.STRING:
            return True

        if self.family is ChTypeFamily.FIXED_STRING:
            return self.length >= old.length

        if self.family in (ChTypeFamily.INT, ChTypeFamily.UINT, ChTypeFamily.FLOAT):
            return self.bits >= old.bits

        if self.family is ChTypeFamily.DECIMAL:
            whole = self.precision - self.scale >= old.precision - old.scale
            return whole and self.scale >= old.scale

        return False


class ChSnapshot(SourceSnapshot):
    """Снимок ClickHouse одной версии: плоские таблицы записей.

    Реализация SourceSnapshot: части и семейства объявлены, инварианты и
    поиск по адресу даёт база; родное здесь — дерево (база → группа →
    объект), карточки, колонки узла по ключам движка и объекты ручного
    источника.
    """

    TABLE_PREFIX: ClassVar[str] = "ch"
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
        for column in self.parts_of_type(ref, PartKind.COLUMN, ChColumn):
            columns.append(
                NodeColumn(
                    name=column.name,
                    type=column.type,
                    nullable=ChNullable.wraps(column.type),
                    key=column.in_primary_key,
                )
            )

        return tuple(columns)

    @classmethod
    def type_widens(cls, old: str, new: str) -> bool:
        return ChTypeName.parse(new).accepts(ChTypeName.parse(old))

    @classmethod
    def tree_scope(cls, path: Sequence[str]) -> TreeScope:
        """Записи для детей пути: базы; таблицы и словари базы (по ним —
        какие группы есть); объекты группы."""
        steps = tuple(path)
        depth = len(steps)
        if depth == ChDepth.DATABASES:
            return TreeScope(parts=(PartScope(part=ChPart.DATABASES),))

        in_database = (("database", steps[0]),)
        if depth == ChDepth.GROUPS:
            return TreeScope(
                parts=(
                    PartScope(part=ChPart.TABLES, where=in_database),
                    PartScope(part=ChPart.DICTIONARIES, where=in_database),
                )
            )

        if depth == ChDepth.OBJECTS:
            group = ChGroup(steps[1])
            return TreeScope(parts=(PartScope(part=group.part(), where=in_database),))

        return TreeScope()

    def children(self, connection_id: UUID, path: Sequence[str]) -> Sequence[TreeNode]:
        """Дети узла дерева по глубине пути: базы, группы, объекты."""
        steps = tuple(path)
        depth = len(steps)
        if depth == ChDepth.DATABASES:
            return list(self._database_nodes())

        if depth == ChDepth.GROUPS:
            return list(self._group_nodes(steps))

        if depth == ChDepth.OBJECTS:
            return list(self._object_nodes(connection_id, steps))

        return []

    def _database_nodes(self) -> Iterator[TreeNode]:
        for database in sorted(self.databases, key=attrgetter("name")):
            yield TreeNode(
                path=database.key,
                label=database.name,
                kind=TreeKind.DATABASE,
                expandable=True,
                detail=database.engine,
                comment=database.comment,
            )

    def _groups_in(self, steps: tuple[str, ...]) -> set[ChGroup]:
        """Группы, в которых у базы есть объекты."""
        groups: set[ChGroup] = set()
        for table in self.tables:
            if table.parent == steps:
                groups.add(ChGroup.of_table(table.kind))

        for dictionary in self.dictionaries:
            if dictionary.parent == steps:
                groups.add(ChGroup.DICTIONARIES)

        return groups

    def _group_nodes(self, steps: tuple[str, ...]) -> Iterator[TreeNode]:
        present = self._groups_in(steps)
        for group in ChGroup:
            if group not in present:
                continue

            yield TreeNode(
                path=(*steps, group.value),
                label=group.value,
                kind=TreeKind.GROUP,
                expandable=True,
            )

    def _object_nodes(
        self, connection_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, group = steps
        if group == ChGroup.DICTIONARIES.value:
            yield from self._dictionary_nodes(connection_id, steps)
            return

        for table in sorted(self.tables, key=attrgetter("name")):
            if table.database != database:
                continue

            if ChGroup.of_table(table.kind).value != group:
                continue

            ref = ObjectRef(
                connection_id=connection_id, kind=ObjectKind.TABLE, path=table.key
            )
            yield TreeNode.object(steps, table.name, ref, table.engine, table.comment)

    def _dictionary_nodes(
        self, connection_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, _group = steps
        for dictionary in sorted(self.dictionaries, key=attrgetter("name")):
            if dictionary.database != database:
                continue

            ref = ObjectRef(
                connection_id=connection_id,
                kind=ObjectKind.DICTIONARY,
                path=dictionary.key,
            )
            yield TreeNode.object(
                steps, dictionary.name, ref, dictionary.layout, dictionary.comment
            )
