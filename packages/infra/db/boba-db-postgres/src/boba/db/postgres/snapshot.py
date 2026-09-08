"""Снимок источника Postgres в родной структуре: базы, схемы, отношения с
колонками, ограничениями и индексами, рутины с аргументами, последовательности,
пользовательские типы. Поля названы как в системных каталогах, значения — как
их отдаёт сервер (тип колонки строкой format_type, определения текстом).

Снимок плоский: каждая таблица хранения — свой кортеж записей с родным
ключом. Дерево для страницы строится из него по запросу.

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
    Records,
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
    "PgCardKind",
    "PgColumn",
    "PgConstraint",
    "PgConstraintKind",
    "PgDatabase",
    "PgGroup",
    "PgIndex",
    "PgPart",
    "PgRelation",
    "PgRelationCard",
    "PgRelationKind",
    "PgRoutine",
    "PgRoutineArg",
    "PgRoutineCard",
    "PgRoutineKind",
    "PgSchema",
    "PgSequence",
    "PgSequenceCard",
    "PgSnapshot",
    "PgSourceKind",
    "PgType",
    "PgTypeCard",
    "PgTypeFamily",
    "PgTypeKind",
    "PgTypeName",
]


class PgRelationKind(StrEnum):
    TABLE = "table"
    PARTITIONED = "partitioned"
    PARTITION = "partition"
    VIEW = "view"
    MATERIALIZED = "materialized"
    FOREIGN = "foreign"


class PgConstraintKind(StrEnum):
    PRIMARY = "primary"
    UNIQUE = "unique"
    FOREIGN = "foreign"
    CHECK = "check"
    EXCLUSION = "exclusion"


class PgRoutineKind(StrEnum):
    FUNCTION = "function"
    PROCEDURE = "procedure"
    AGGREGATE = "aggregate"
    WINDOW = "window"


class PgTypeKind(StrEnum):
    ENUM = "enum"
    DOMAIN = "domain"
    COMPOSITE = "composite"
    RANGE = "range"


class PgDepth(IntEnum):
    """Глубина пути в дереве Postgres: что лежит на этом уровне."""

    DATABASES = 0
    SCHEMAS = 1
    GROUPS = 2
    OBJECTS = 3
    PARTITIONS = 4


class PgGroup(StrEnum):
    """Группы объектов под схемой в дереве; значение — ступень пути."""

    TABLES = "tables"
    VIEWS = "views"
    MATERIALIZED = "materialized"
    FOREIGN = "foreign"
    FUNCTIONS = "functions"
    PROCEDURES = "procedures"
    SEQUENCES = "sequences"
    TYPES = "types"

    @classmethod
    def of_relation(cls, kind: PgRelationKind) -> PgGroup:
        if kind is PgRelationKind.VIEW:
            return cls.VIEWS

        if kind is PgRelationKind.MATERIALIZED:
            return cls.MATERIALIZED

        if kind is PgRelationKind.FOREIGN:
            return cls.FOREIGN

        return cls.TABLES

    @classmethod
    def of_routine(cls, kind: PgRoutineKind) -> PgGroup:
        if kind is PgRoutineKind.PROCEDURE:
            return cls.PROCEDURES

        return cls.FUNCTIONS

    def part(self) -> str:
        """Часть снимка, в которой лежат объекты группы."""
        if self in (PgGroup.FUNCTIONS, PgGroup.PROCEDURES):
            return PgPart.ROUTINES

        if self is PgGroup.SEQUENCES:
            return PgPart.SEQUENCES

        if self is PgGroup.TYPES:
            return PgPart.TYPES

        return PgPart.RELATIONS


class PgDatabase(SourceRecord):
    name: str = Field(min_length=1)
    owner: str = ""
    encoding: str = ""
    collate: str = ""
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("name",)
    PARENT: ClassVar[tuple[str, ...]] = ()


class PgSchema(SourceRecord):
    database: str = Field(min_length=1)
    name: str = Field(min_length=1)
    owner: str = ""
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("database", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database",)


class PgRelation(SourceObject):
    """Таблица, секционированная таблица, секция, представление,
    материализованное представление или foreign-таблица."""

    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: PgRelationKind
    owner: str = ""
    comment: str | None = None
    tablespace: str | None = None
    persistence: str = "permanent"
    row_estimate: int = 0
    total_bytes: int = 0
    VOLATILE: ClassVar[frozenset[str]] = frozenset({"row_estimate", "total_bytes"})
    partition_key: str | None = None
    partition_of: str | None = None
    partition_bound: str | None = None
    definition: str | None = None
    check_option: str | None = None
    populated: bool | None = None
    foreign_server: str | None = None
    options: dict[str, str] = Field(default_factory=dict)

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name")
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.RELATION

    @property
    def partition_label(self) -> str:
        """Как секции называют родителя: schema.name."""
        return f"{self.schema_name}.{self.name}"

    @property
    def label(self) -> str:
        return self.name

    def partitions_in(self, snapshot: SourceSnapshot) -> tuple[PgRelation, ...]:
        """Секции этой таблицы в снимке по имени, отсортированные."""
        siblings = Records.of_type(snapshot.records_of(PgPart.RELATIONS), PgRelation)
        partitions: list[PgRelation] = []
        for relation in siblings:
            if relation.kind is not PgRelationKind.PARTITION:
                continue

            if relation.database != self.database:
                continue

            if relation.partition_of != self.partition_label:
                continue

            partitions.append(relation)

        partitions.sort(key=attrgetter("name"))
        return tuple(partitions)

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        partitions = self.partitions_in(snapshot)
        return PgRelationCard(
            ref=ref,
            relation=self,
            columns=snapshot.parts_of_type(ref, PartKind.COLUMN, PgColumn),
            constraints=snapshot.parts_of_type(ref, PartKind.CONSTRAINT, PgConstraint),
            indexes=snapshot.parts_of_type(ref, PartKind.INDEX, PgIndex),
            partitions=tuple(partitions),
        )


class PgColumn(SourceRecord):
    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    type: str = Field(min_length=1)
    nullable: bool = True
    default: str | None = None
    identity: str | None = None
    generated: str | None = None
    collation: str | None = None
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "relation", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name", "relation")
    ORDER: ClassVar[tuple[str, ...]] = ("ordinal",)
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}


class PgConstraint(SourceRecord):
    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: PgConstraintKind
    columns: tuple[str, ...] = ()
    ref_schema: str | None = None
    ref_relation: str | None = None
    ref_columns: tuple[str, ...] | None = None
    on_update: str | None = None
    on_delete: str | None = None
    deferrable: bool = False
    initially_deferred: bool = False
    definition: str = ""
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "relation", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name", "relation")
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}


class PgIndex(SourceRecord):
    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    name: str = Field(min_length=1)
    method: str = "btree"
    unique: bool = False
    primary: bool = False
    columns: tuple[str, ...] = ()
    predicate: str | None = None
    definition: str = ""
    total_bytes: int = 0
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "relation", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name", "relation")
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}
    VOLATILE: ClassVar[frozenset[str]] = frozenset({"total_bytes"})


class PgRoutine(SourceObject):
    """Функция, процедура, агрегат или оконная функция; перегрузки различаются
    сигнатурой, она входит в ключ и в адрес."""

    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    signature: str = ""
    kind: PgRoutineKind
    owner: str = ""
    language: str = ""
    arguments: str = ""
    returns: str | None = None
    returns_set: bool = False
    volatility: str = "volatile"
    strict: bool = False
    security_definer: bool = False
    parallel: str = "unsafe"
    cost: float = 100.0
    rows: float | None = None
    body: str = ""
    definition: str = ""
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "name", "signature")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name")
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.ROUTINE

    @property
    def label(self) -> str:
        return f"{self.name}({self.signature})"

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        return PgRoutineCard(
            ref=ref,
            routine=self,
            arguments=snapshot.parts_of_type(ref, PartKind.ARGUMENT, PgRoutineArg),
        )


class PgRoutineArg(SourceRecord):
    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    routine: str = Field(min_length=1)
    signature: str = ""
    position: int = Field(ge=0)
    name: str | None = None
    type: str = Field(min_length=1)
    mode: str = "in"
    default: str | None = None

    KEY: ClassVar[tuple[str, ...]] = (
        "database",
        "schema_name",
        "routine",
        "signature",
        "position",
    )
    PARENT: ClassVar[tuple[str, ...]] = (
        "database",
        "schema_name",
        "routine",
        "signature",
    )
    ORDER: ClassVar[tuple[str, ...]] = ("position",)
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}


class PgSequence(SourceObject):
    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str = "bigint"
    start: int = 1
    minimum: int = 1
    maximum: int = 9223372036854775807
    increment: int = 1
    cycle: bool = False
    cache: int = 1
    last_value: int | None = None
    owned_by: str | None = None
    comment: str | None = None
    VOLATILE: ClassVar[frozenset[str]] = frozenset({"last_value"})

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name")
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.SEQUENCE

    @property
    def label(self) -> str:
        return self.name

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        return PgSequenceCard(ref=ref, sequence=self)


class PgTypeAttribute(CatalogModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)


class PgType(SourceObject):
    database: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: PgTypeKind
    owner: str = ""
    labels: tuple[str, ...] | None = None
    base_type: str | None = None
    constraint: str | None = None
    attributes: tuple[PgTypeAttribute, ...] | None = None
    comment: str | None = None

    KEY: ClassVar[tuple[str, ...]] = ("database", "schema_name", "name")
    PARENT: ClassVar[tuple[str, ...]] = ("database", "schema_name")
    COLUMN_NAMES: ClassVar[Mapping[str, str]] = {"schema_name": "schema"}

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.TYPE

    @property
    def label(self) -> str:
        return self.name

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        return PgTypeCard(ref=ref, type=self)


class PgPart(StrEnum):
    """Части снимка Postgres: имена полей PgSnapshot."""

    DATABASES = "databases"
    SCHEMAS = "schemas"
    RELATIONS = "relations"
    COLUMNS = "columns"
    CONSTRAINTS = "constraints"
    INDEXES = "indexes"
    ROUTINES = "routines"
    ROUTINE_ARGS = "routine_args"
    SEQUENCES = "sequences"
    TYPES = "types"


class PgSourceKind(StrEnum):
    """kind типа соединения, которым владеет этот пакет; тем же именем снимок
    зарегистрирован в группе boba.catalog."""

    POSTGRES = "postgres"


class PgCardKind(StrEnum):
    """Дискриминаторы карточек объектов этого вида источника."""

    PG_RELATION = "pg_relation"
    PG_ROUTINE = "pg_routine"
    PG_SEQUENCE = "pg_sequence"
    PG_TYPE = "pg_type"


class PgRelationCard(ObjectCard):
    card: Literal[PgCardKind.PG_RELATION] = PgCardKind.PG_RELATION
    relation: PgRelation
    columns: tuple[PgColumn, ...]
    constraints: tuple[PgConstraint, ...]
    indexes: tuple[PgIndex, ...]
    partitions: tuple[PgRelation, ...]


class PgRoutineCard(ObjectCard):
    card: Literal[PgCardKind.PG_ROUTINE] = PgCardKind.PG_ROUTINE
    routine: PgRoutine
    arguments: tuple[PgRoutineArg, ...]


class PgSequenceCard(ObjectCard):
    card: Literal[PgCardKind.PG_SEQUENCE] = PgCardKind.PG_SEQUENCE
    sequence: PgSequence


class PgTypeCard(ObjectCard):
    card: Literal[PgCardKind.PG_TYPE] = PgCardKind.PG_TYPE
    type: PgType


class PgTypeFamily(StrEnum):
    """Семейства типов Postgres, внутри которых тип бывает шире другого."""

    VARCHAR = "varchar"
    CHAR = "char"
    TEXT = "text"
    NUMERIC = "numeric"
    INTEGER = "integer"
    FLOAT = "float"
    TIMESTAMP = "timestamp"
    TIMESTAMPTZ = "timestamptz"
    OTHER = "other"


class PgTypeName(CatalogModel):
    """Тип колонки Postgres, как его пишет format_type: семейство, модификаторы
    (длина, точность и масштаб, точность времени) и ранг внутри семейства.
    Знает, шире ли один тип другого; незнакомые типы сравниваются как строки."""

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?P<name>[a-z ]+?)\s*(?:\((?P<mods>[^)]*)\))?\s*"
        r"(?P<tail>with time zone|without time zone)?$"
    )
    FAMILIES: ClassVar[Mapping[str, PgTypeFamily]] = {
        "character varying": PgTypeFamily.VARCHAR,
        "varchar": PgTypeFamily.VARCHAR,
        "character": PgTypeFamily.CHAR,
        "char": PgTypeFamily.CHAR,
        "bpchar": PgTypeFamily.CHAR,
        "text": PgTypeFamily.TEXT,
        "numeric": PgTypeFamily.NUMERIC,
        "decimal": PgTypeFamily.NUMERIC,
        "smallint": PgTypeFamily.INTEGER,
        "int2": PgTypeFamily.INTEGER,
        "integer": PgTypeFamily.INTEGER,
        "int": PgTypeFamily.INTEGER,
        "int4": PgTypeFamily.INTEGER,
        "bigint": PgTypeFamily.INTEGER,
        "int8": PgTypeFamily.INTEGER,
        "real": PgTypeFamily.FLOAT,
        "float4": PgTypeFamily.FLOAT,
        "double precision": PgTypeFamily.FLOAT,
        "float8": PgTypeFamily.FLOAT,
        "timestamp": PgTypeFamily.TIMESTAMP,
        "timestamp without time zone": PgTypeFamily.TIMESTAMP,
        "timestamptz": PgTypeFamily.TIMESTAMPTZ,
        "timestamp with time zone": PgTypeFamily.TIMESTAMPTZ,
    }
    RANKS: ClassVar[Mapping[str, int]] = {
        "smallint": 1,
        "int2": 1,
        "integer": 2,
        "int": 2,
        "int4": 2,
        "bigint": 3,
        "int8": 3,
        "real": 1,
        "float4": 1,
        "double precision": 2,
        "float8": 2,
    }
    UNBOUNDED: ClassVar[int] = 10**9
    """Длина или точность без ограничения: «шире любого числа»."""
    TIMESTAMP_PRECISION: ClassVar[int] = 6

    raw: str
    family: PgTypeFamily
    rank: int = 0
    length: int = UNBOUNDED
    precision: int = UNBOUNDED
    scale: int = UNBOUNDED

    @classmethod
    def parse(cls, raw: str) -> PgTypeName:
        text = raw.strip().lower()
        match = cls.PATTERN.match(text)
        if match is None:
            return cls(raw=text, family=PgTypeFamily.OTHER)

        name = match.group("name").strip()
        tail = match.group("tail")
        if tail is not None:
            name = f"{name} {tail}"

        mods = cls._mods(match.group("mods"))
        family = cls.FAMILIES.get(name, PgTypeFamily.OTHER)
        return cls(
            raw=text,
            family=family,
            rank=cls.RANKS.get(name, 0),
            length=cls._at(mods, 0, cls.UNBOUNDED),
            precision=cls._precision(family, mods),
            scale=cls._scale(family, mods),
        )

    @classmethod
    def _precision(cls, family: PgTypeFamily, mods: tuple[int, ...]) -> int:
        if family in (PgTypeFamily.TIMESTAMP, PgTypeFamily.TIMESTAMPTZ):
            return cls._at(mods, 0, cls.TIMESTAMP_PRECISION)

        return cls._at(mods, 0, cls.UNBOUNDED)

    @classmethod
    def _scale(cls, family: PgTypeFamily, mods: tuple[int, ...]) -> int:
        """numeric(p) — масштаб 0; numeric без модификаторов принимает любой."""
        if family is not PgTypeFamily.NUMERIC:
            return 0

        if not mods:
            return cls.UNBOUNDED

        return cls._at(mods, 1, 0)

    @staticmethod
    def _mods(raw: str | None) -> tuple[int, ...]:
        if raw is None:
            return ()

        values: list[int] = []
        for piece in raw.split(","):
            stripped = piece.strip()
            if not stripped.isdigit():
                return ()

            values.append(int(stripped))

        return tuple(values)

    @staticmethod
    def _at(mods: tuple[int, ...], index: int, default: int) -> int:
        if index < len(mods):
            return mods[index]

        return default

    def accepts(self, old: PgTypeName) -> bool:
        """Этот тип принимает всё, что принимал old: равен ему или шире."""
        if self.raw == old.raw:
            return True

        if old.family is PgTypeFamily.VARCHAR and self.family is PgTypeFamily.TEXT:
            return True

        if self.family is not old.family:
            return False

        return self._wider_in_family(old)

    def _wider_in_family(self, old: PgTypeName) -> bool:
        if self.family in (PgTypeFamily.VARCHAR, PgTypeFamily.CHAR):
            return self.length >= old.length

        if self.family is PgTypeFamily.NUMERIC:
            return self._wider_numeric(old)

        if self.family in (PgTypeFamily.INTEGER, PgTypeFamily.FLOAT):
            return self.rank >= old.rank

        if self.family in (PgTypeFamily.TIMESTAMP, PgTypeFamily.TIMESTAMPTZ):
            return self.precision >= old.precision

        return False

    def _wider_numeric(self, old: PgTypeName) -> bool:
        """numeric без модификаторов шире любого; целая часть и масштаб не
        сужаются."""
        if self.precision == self.UNBOUNDED:
            return True

        if old.precision == self.UNBOUNDED:
            return False

        whole = self.precision - self.scale >= old.precision - old.scale
        return whole and self.scale >= old.scale


class PgSnapshot(SourceSnapshot):
    """Снимок Postgres одной версии: плоские таблицы записей.

    Реализация SourceSnapshot: части и семейства объявлены, инварианты и
    поиск по адресу даёт база; родное здесь — дерево (база → схема → группа
    → объект, у секционированной таблицы дети — секции), карточки, колонки
    узла с первичным ключом из ограничений и объекты ручного источника.
    """

    TABLE_PREFIX: ClassVar[str] = "pg"
    SYNC_TOOL: ClassVar[str] = "pg_schema_snapshot"
    OBJECT_PARTS: ClassVar[tuple[str, ...]] = (
        PgPart.RELATIONS,
        PgPart.ROUTINES,
        PgPart.SEQUENCES,
        PgPart.TYPES,
    )
    """Части с объектами под схемой: по ним дерево узнаёт группы схемы."""
    PARTS: ClassVar[tuple[SnapshotPart, ...]] = (
        SnapshotPart(name=PgPart.DATABASES, model=PgDatabase, label="database"),
        SnapshotPart(
            name=PgPart.SCHEMAS,
            model=PgSchema,
            label="schema",
            parent=PgPart.DATABASES,
        ),
        SnapshotPart(
            name=PgPart.RELATIONS,
            model=PgRelation,
            label="relation",
            parent=PgPart.SCHEMAS,
        ),
        SnapshotPart(
            name=PgPart.COLUMNS,
            model=PgColumn,
            label="column",
            parent=PgPart.RELATIONS,
        ),
        SnapshotPart(
            name=PgPart.CONSTRAINTS,
            model=PgConstraint,
            label="constraint",
            parent=PgPart.RELATIONS,
        ),
        SnapshotPart(
            name=PgPart.INDEXES,
            model=PgIndex,
            label="index",
            parent=PgPart.RELATIONS,
        ),
        SnapshotPart(
            name=PgPart.ROUTINES,
            model=PgRoutine,
            label="routine",
            parent=PgPart.SCHEMAS,
        ),
        SnapshotPart(
            name=PgPart.ROUTINE_ARGS,
            model=PgRoutineArg,
            label="routine argument",
            parent=PgPart.ROUTINES,
        ),
        SnapshotPart(
            name=PgPart.SEQUENCES,
            model=PgSequence,
            label="sequence",
            parent=PgPart.SCHEMAS,
        ),
        SnapshotPart(
            name=PgPart.TYPES, model=PgType, label="type", parent=PgPart.SCHEMAS
        ),
    )
    FAMILIES: ClassVar[tuple[ObjectFamily, ...]] = (
        ObjectFamily(
            kind=ObjectKind.RELATION,
            part=PgPart.RELATIONS,
            subparts=(
                SubPart(kind=PartKind.COLUMN, part=PgPart.COLUMNS),
                SubPart(kind=PartKind.CONSTRAINT, part=PgPart.CONSTRAINTS),
                SubPart(kind=PartKind.INDEX, part=PgPart.INDEXES),
            ),
        ),
        ObjectFamily(
            kind=ObjectKind.ROUTINE,
            part=PgPart.ROUTINES,
            subparts=(SubPart(kind=PartKind.ARGUMENT, part=PgPart.ROUTINE_ARGS),),
        ),
        ObjectFamily(kind=ObjectKind.SEQUENCE, part=PgPart.SEQUENCES),
        ObjectFamily(kind=ObjectKind.TYPE, part=PgPart.TYPES),
    )

    kind: Literal[PgSourceKind.POSTGRES] = PgSourceKind.POSTGRES
    databases: tuple[PgDatabase, ...] = ()
    schemas: tuple[PgSchema, ...] = ()
    relations: tuple[PgRelation, ...] = ()
    columns: tuple[PgColumn, ...] = ()
    constraints: tuple[PgConstraint, ...] = ()
    indexes: tuple[PgIndex, ...] = ()
    routines: tuple[PgRoutine, ...] = ()
    routine_args: tuple[PgRoutineArg, ...] = ()
    sequences: tuple[PgSequence, ...] = ()
    types: tuple[PgType, ...] = ()

    def node_columns(self, ref: ObjectRef) -> tuple[NodeColumn, ...]:
        if ref.kind is not ObjectKind.RELATION:
            return ()

        keys: set[str] = set()
        for constraint in self.parts_of_type(ref, PartKind.CONSTRAINT, PgConstraint):
            if constraint.kind is not PgConstraintKind.PRIMARY:
                continue

            keys.update(constraint.columns)

        columns: list[NodeColumn] = []
        for column in self.parts_of_type(ref, PartKind.COLUMN, PgColumn):
            columns.append(
                NodeColumn(
                    name=column.name,
                    type=column.type,
                    nullable=column.nullable,
                    key=column.name in keys,
                )
            )

        return tuple(columns)

    @classmethod
    def type_widens(cls, old: str, new: str) -> bool:
        return PgTypeName.parse(new).accepts(PgTypeName.parse(old))

    @classmethod
    def tree_scope(cls, path: Sequence[str]) -> TreeScope:
        """Записи для детей пути: базы; схемы базы; объекты схемы (по ним —
        какие группы есть); объекты группы; секции таблицы — по родителю и по
        partition_of в той же базе."""
        steps = tuple(path)
        depth = len(steps)
        if depth == PgDepth.DATABASES:
            return TreeScope(parts=(PartScope(part=PgPart.DATABASES),))

        if depth == PgDepth.SCHEMAS:
            where = (("database", steps[0]),)
            return TreeScope(parts=(PartScope(part=PgPart.SCHEMAS, where=where),))

        in_schema = (("database", steps[0]), ("schema_name", steps[1]))
        if depth == PgDepth.GROUPS:
            parts: list[PartScope] = []
            for part in cls.OBJECT_PARTS:
                parts.append(PartScope(part=part, where=in_schema))

            return TreeScope(parts=tuple(parts))

        if depth == PgDepth.OBJECTS:
            group = PgGroup(steps[2])
            return TreeScope(parts=(PartScope(part=group.part(), where=in_schema),))

        partitions_level = depth == PgDepth.PARTITIONS
        under_tables = partitions_level and steps[2] == PgGroup.TABLES.value
        if not under_tables:
            return TreeScope()

        parent = (*in_schema, ("name", steps[3]))
        of_parent = (("database", steps[0]), ("partition_of", f"{steps[1]}.{steps[3]}"))
        return TreeScope(
            parts=(
                PartScope(part=PgPart.RELATIONS, where=parent),
                PartScope(part=PgPart.RELATIONS, where=of_parent),
            )
        )

    def children(self, connection_id: UUID, path: Sequence[str]) -> Sequence[TreeNode]:
        """Дети узла дерева по глубине пути: базы, схемы, группы, объекты,
        секции таблицы."""
        steps = tuple(path)
        depth = len(steps)
        if depth == PgDepth.DATABASES:
            return list(self._database_nodes())

        if depth == PgDepth.SCHEMAS:
            return list(self._schema_nodes(steps[0]))

        if depth == PgDepth.GROUPS:
            return list(self._group_nodes(steps))

        if depth == PgDepth.OBJECTS:
            return list(self._object_nodes(connection_id, steps))

        partitions_level = depth == PgDepth.PARTITIONS
        under_tables = partitions_level and steps[2] == PgGroup.TABLES.value
        if not under_tables:
            return []

        return list(self._partition_nodes(connection_id, steps))

    def _database_nodes(self) -> Iterator[TreeNode]:
        for database in sorted(self.databases, key=attrgetter("name")):
            yield TreeNode(
                path=(database.name,),
                label=database.name,
                kind=TreeKind.DATABASE,
                expandable=True,
                comment=database.comment,
            )

    def _schemas_in(self, database: str) -> Iterator[PgSchema]:
        for schema in sorted(self.schemas, key=attrgetter("name")):
            if schema.database != database:
                continue

            yield schema

    def _schema_nodes(self, database: str) -> Iterator[TreeNode]:
        for schema in self._schemas_in(database):
            yield TreeNode(
                path=(database, schema.name),
                label=schema.name,
                kind=TreeKind.SCHEMA,
                expandable=True,
                comment=schema.comment,
            )

    def _groups_in(self, steps: tuple[str, ...]) -> set[PgGroup]:
        """Группы, в которых у схемы есть объекты."""
        groups: set[PgGroup] = set()
        for relation in self.relations:
            if relation.parent != steps:
                continue

            if relation.kind is PgRelationKind.PARTITION:
                continue

            groups.add(PgGroup.of_relation(relation.kind))

        for routine in self.routines:
            if routine.parent == steps:
                groups.add(PgGroup.of_routine(routine.kind))

        for sequence in self.sequences:
            if sequence.parent == steps:
                groups.add(PgGroup.SEQUENCES)

        for typ in self.types:
            if typ.parent == steps:
                groups.add(PgGroup.TYPES)

        return groups

    def _group_nodes(self, steps: tuple[str, ...]) -> Iterator[TreeNode]:
        present = self._groups_in(steps)
        for group in PgGroup:
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
        database, schema, group = steps
        if group in (PgGroup.SEQUENCES.value, PgGroup.TYPES.value):
            yield from self._plain_object_nodes(connection_id, steps)
            return

        if group in (PgGroup.FUNCTIONS.value, PgGroup.PROCEDURES.value):
            yield from self._routine_nodes(connection_id, steps)
            return

        for relation in sorted(self.relations, key=attrgetter("name")):
            if relation.key[:2] != (database, schema):
                continue

            if relation.kind is PgRelationKind.PARTITION:
                continue

            if PgGroup.of_relation(relation.kind).value != group:
                continue

            ref = ObjectRef(
                connection_id=connection_id, kind=ObjectKind.RELATION, path=relation.key
            )
            node = TreeNode.object(
                steps, relation.name, ref, relation.kind.value, relation.comment
            )
            expandable = relation.kind is PgRelationKind.PARTITIONED
            yield node.model_copy(update={"expandable": expandable})

    def _partition_nodes(
        self, connection_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, schema, _group, name = steps
        parent = self.object_at(
            ObjectRef(
                connection_id=connection_id,
                kind=ObjectKind.RELATION,
                path=(database, schema, name),
            )
        )
        if not isinstance(parent, PgRelation):
            return

        for partition in parent.partitions_in(self):
            detail = partition.partition_bound
            if not detail:
                detail = PgRelationKind.PARTITION.value

            ref = ObjectRef(
                connection_id=connection_id,
                kind=ObjectKind.RELATION,
                path=partition.key,
            )
            yield TreeNode.object(steps, partition.name, ref, detail, partition.comment)

    def _routine_nodes(
        self, connection_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, schema, group = steps
        for routine in sorted(self.routines, key=attrgetter("name", "signature")):
            if routine.key[:2] != (database, schema):
                continue

            if PgGroup.of_routine(routine.kind).value != group:
                continue

            detail = routine.returns
            if not detail:
                detail = routine.kind.value

            ref = ObjectRef(
                connection_id=connection_id, kind=ObjectKind.ROUTINE, path=routine.key
            )
            yield TreeNode.object(steps, routine.label, ref, detail, routine.comment)

    def _plain_object_nodes(
        self, connection_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, schema, group = steps
        if group == PgGroup.SEQUENCES.value:
            for sequence in sorted(self.sequences, key=attrgetter("name")):
                if sequence.key[:2] != (database, schema):
                    continue

                ref = ObjectRef(
                    connection_id=connection_id,
                    kind=ObjectKind.SEQUENCE,
                    path=sequence.key,
                )
                yield TreeNode.object(
                    steps, sequence.name, ref, sequence.type, sequence.comment
                )
            return

        for typ in sorted(self.types, key=attrgetter("name")):
            if typ.key[:2] != (database, schema):
                continue

            ref = ObjectRef(
                connection_id=connection_id, kind=ObjectKind.TYPE, path=typ.key
            )
            yield TreeNode.object(steps, typ.name, ref, typ.kind.value, typ.comment)
