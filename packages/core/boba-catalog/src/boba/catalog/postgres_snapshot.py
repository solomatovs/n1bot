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
    "PgColumn",
    "PgConstraint",
    "PgConstraintKind",
    "PgDatabase",
    "PgGroup",
    "PgIndex",
    "PgRelation",
    "PgRelationKind",
    "PgRoutine",
    "PgRoutineArg",
    "PgRoutineKind",
    "PgSchema",
    "PgSequence",
    "PgSnapshot",
    "PgType",
    "PgTypeKind",
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


class PgDatabase(SourceRecord):
    name: str = Field(min_length=1)
    owner: str = ""
    encoding: str = ""
    collate: str = ""
    comment: str | None = None

    @property
    def key(self) -> tuple[str, ...]:
        return (self.name,)

    @property
    def parent(self) -> tuple[str, ...]:
        return ()


class PgSchema(SourceRecord):
    database: str = Field(min_length=1)
    name: str = Field(min_length=1)
    owner: str = ""
    comment: str | None = None

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database,)


class PgRelation(SourceRecord):
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
    partition_key: str | None = None
    partition_of: str | None = None
    partition_bound: str | None = None
    definition: str | None = None
    check_option: str | None = None
    populated: bool | None = None
    foreign_server: str | None = None
    options: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.RELATION

    @property
    def label(self) -> str:
        return self.name


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

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.relation, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.relation)


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

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.relation, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.relation)


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

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.relation, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.relation)


class PgRoutine(SourceRecord):
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

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.name, self.signature)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.ROUTINE

    @property
    def label(self) -> str:
        return f"{self.name}({self.signature})"


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

    @property
    def key(self) -> tuple[str, ...]:
        return (
            self.database,
            self.schema_name,
            self.routine,
            self.signature,
            str(self.position),
        )

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.routine, self.signature)


class PgSequence(SourceRecord):
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

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.SEQUENCE

    @property
    def label(self) -> str:
        return self.name


class PgTypeAttribute(CatalogModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)


class PgType(SourceRecord):
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

    @property
    def key(self) -> tuple[str, ...]:
        return (self.database, self.schema_name, self.name)

    @property
    def parent(self) -> tuple[str, ...]:
        return (self.database, self.schema_name)

    @property
    def object_kind(self) -> ObjectKind:
        return ObjectKind.TYPE

    @property
    def label(self) -> str:
        return self.name


class PgSnapshot(CatalogModel):
    """Снимок Postgres одной версии: плоские таблицы записей.

    Проверяет инварианты целиком (check), находит объект по адресу,
    раскрывается в дерево любой глубины: база → схема → группа → объект,
    у секционированной таблицы дети — её секции.
    """

    kind: Literal[SourceKind.POSTGRES] = SourceKind.POSTGRES
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

    @classmethod
    def empty(cls) -> PgSnapshot:
        return cls()

    def check(self) -> None:
        """Ключи уникальны, у каждой записи есть родитель.

        Ошибки:
        CatalogInvariantError — с перечнем нарушений.
        """
        Keyed.require_unique("database", self.databases)
        Keyed.require_unique("schema", self.schemas)
        Keyed.require_unique("relation", self.relations)
        Keyed.require_unique("column", self.columns)
        Keyed.require_unique("constraint", self.constraints)
        Keyed.require_unique("index", self.indexes)
        Keyed.require_unique("routine", self.routines)
        Keyed.require_unique("routine argument", self.routine_args)
        Keyed.require_unique("sequence", self.sequences)
        Keyed.require_unique("type", self.types)

        databases = Keyed.keys_of(self.databases)
        schemas = Keyed.keys_of(self.schemas)
        relations = Keyed.keys_of(self.relations)
        routines = Keyed.keys_of(self.routines)
        Keyed.require_parents("schema", self.schemas, databases)
        Keyed.require_parents("relation", self.relations, schemas)
        Keyed.require_parents("column", self.columns, relations)
        Keyed.require_parents("constraint", self.constraints, relations)
        Keyed.require_parents("index", self.indexes, relations)
        Keyed.require_parents("routine", self.routines, schemas)
        Keyed.require_parents("routine argument", self.routine_args, routines)
        Keyed.require_parents("sequence", self.sequences, schemas)
        Keyed.require_parents("type", self.types, schemas)

    def objects_count(self) -> int:
        count = len(self.relations) + len(self.routines)
        count += len(self.sequences) + len(self.types)
        return count

    def relation(self, path: Sequence[str]) -> PgRelation | None:
        for relation in self.relations:
            if relation.key == tuple(path):
                return relation

        return None

    def routine(self, path: Sequence[str]) -> PgRoutine | None:
        for routine in self.routines:
            if routine.key == tuple(path):
                return routine

        return None

    def sequence(self, path: Sequence[str]) -> PgSequence | None:
        for sequence in self.sequences:
            if sequence.key == tuple(path):
                return sequence

        return None

    def type(self, path: Sequence[str]) -> PgType | None:
        for typ in self.types:
            if typ.key == tuple(path):
                return typ

        return None

    def columns_of(self, path: Sequence[str]) -> Iterator[PgColumn]:
        wanted = tuple(path)
        for column in sorted(self.columns, key=attrgetter("ordinal")):
            if column.parent != wanted:
                continue

            yield column

    def constraints_of(self, path: Sequence[str]) -> Iterator[PgConstraint]:
        wanted = tuple(path)
        for constraint in self.constraints:
            if constraint.parent != wanted:
                continue

            yield constraint

    def indexes_of(self, path: Sequence[str]) -> Iterator[PgIndex]:
        wanted = tuple(path)
        for index in self.indexes:
            if index.parent != wanted:
                continue

            yield index

    def args_of(self, path: Sequence[str]) -> Iterator[PgRoutineArg]:
        wanted = tuple(path)
        for arg in sorted(self.routine_args, key=attrgetter("position")):
            if arg.parent != wanted:
                continue

            yield arg

    def partitions_of(self, path: Sequence[str]) -> Iterator[PgRelation]:
        parent = tuple(path)
        parent_label = f"{parent[1]}.{parent[2]}"
        for relation in self.relations:
            if relation.kind is not PgRelationKind.PARTITION:
                continue

            if relation.database != parent[0]:
                continue

            if relation.partition_of != parent_label:
                continue

            yield relation

    def children(self, source_id: UUID, path: Sequence[str]) -> Sequence[TreeNode]:
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
            return list(self._object_nodes(source_id, steps))

        partitions_level = depth == PgDepth.PARTITIONS
        under_tables = partitions_level and steps[2] == PgGroup.TABLES.value
        if not under_tables:
            return []

        return list(self._partition_nodes(source_id, steps))

    def _database_nodes(self) -> Iterator[TreeNode]:
        for database in sorted(self.databases, key=attrgetter("name")):
            count = len(list(self._schemas_in(database.name)))
            yield TreeNode(
                path=(database.name,),
                label=database.name,
                kind=TreeKind.DATABASE,
                children_count=count,
                comment=database.comment,
            )

    def _schemas_in(self, database: str) -> Iterator[PgSchema]:
        for schema in sorted(self.schemas, key=attrgetter("name")):
            if schema.database != database:
                continue

            yield schema

    def _schema_nodes(self, database: str) -> Iterator[TreeNode]:
        for schema in self._schemas_in(database):
            groups = list(self._group_nodes((database, schema.name)))
            yield TreeNode(
                path=(database, schema.name),
                label=schema.name,
                kind=TreeKind.SCHEMA,
                children_count=len(groups),
                comment=schema.comment,
            )

    def _group_counts(self, steps: tuple[str, ...]) -> dict[PgGroup, int]:
        counts: dict[PgGroup, int] = {}
        for relation in self.relations:
            if relation.parent != steps:
                continue

            if relation.kind is PgRelationKind.PARTITION:
                continue

            group = PgGroup.of_relation(relation.kind)
            counts[group] = counts.get(group, 0) + 1

        for routine in self.routines:
            if routine.parent != steps:
                continue

            group = PgGroup.of_routine(routine.kind)
            counts[group] = counts.get(group, 0) + 1

        for sequence in self.sequences:
            if sequence.parent != steps:
                continue

            counts[PgGroup.SEQUENCES] = counts.get(PgGroup.SEQUENCES, 0) + 1

        for typ in self.types:
            if typ.parent != steps:
                continue

            counts[PgGroup.TYPES] = counts.get(PgGroup.TYPES, 0) + 1

        return counts

    def _group_nodes(self, steps: tuple[str, ...]) -> Iterator[TreeNode]:
        counts = self._group_counts(steps)
        for group in PgGroup:
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
        database, schema, group = steps
        if group in (PgGroup.SEQUENCES.value, PgGroup.TYPES.value):
            yield from self._plain_object_nodes(source_id, steps)
            return

        if group in (PgGroup.FUNCTIONS.value, PgGroup.PROCEDURES.value):
            yield from self._routine_nodes(source_id, steps)
            return

        for relation in sorted(self.relations, key=attrgetter("name")):
            if relation.key[:2] != (database, schema):
                continue

            if relation.kind is PgRelationKind.PARTITION:
                continue

            if PgGroup.of_relation(relation.kind).value != group:
                continue

            partitions = 0
            if relation.kind is PgRelationKind.PARTITIONED:
                partitions = len(list(self.partitions_of(relation.key)))

            yield TreeNode(
                path=(*steps, relation.name),
                label=relation.name,
                kind=TreeKind.OBJECT,
                children_count=partitions,
                detail=relation.kind.value,
                comment=relation.comment,
                ref=ObjectRef(
                    source_id=source_id, kind=ObjectKind.RELATION, path=relation.key
                ),
            )

    def _partition_nodes(
        self, source_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, schema, _group, name = steps
        for partition in sorted(
            self.partitions_of((database, schema, name)), key=attrgetter("name")
        ):
            yield TreeNode(
                path=(*steps, partition.name),
                label=partition.name,
                kind=TreeKind.OBJECT,
                children_count=0,
                detail=partition.partition_bound or PgRelationKind.PARTITION.value,
                comment=partition.comment,
                ref=ObjectRef(
                    source_id=source_id, kind=ObjectKind.RELATION, path=partition.key
                ),
            )

    def _routine_nodes(
        self, source_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, schema, group = steps
        for routine in sorted(self.routines, key=attrgetter("name", "signature")):
            if routine.key[:2] != (database, schema):
                continue

            if PgGroup.of_routine(routine.kind).value != group:
                continue

            yield TreeNode(
                path=(*steps, routine.label),
                label=routine.label,
                kind=TreeKind.OBJECT,
                children_count=0,
                detail=routine.returns or routine.kind.value,
                comment=routine.comment,
                ref=ObjectRef(
                    source_id=source_id, kind=ObjectKind.ROUTINE, path=routine.key
                ),
            )

    def _plain_object_nodes(
        self, source_id: UUID, steps: tuple[str, ...]
    ) -> Iterator[TreeNode]:
        database, schema, group = steps
        if group == PgGroup.SEQUENCES.value:
            for sequence in sorted(self.sequences, key=attrgetter("name")):
                if sequence.key[:2] != (database, schema):
                    continue

                yield TreeNode(
                    path=(*steps, sequence.name),
                    label=sequence.name,
                    kind=TreeKind.OBJECT,
                    children_count=0,
                    detail=sequence.type,
                    comment=sequence.comment,
                    ref=ObjectRef(
                        source_id=source_id, kind=ObjectKind.SEQUENCE, path=sequence.key
                    ),
                )
            return

        for typ in sorted(self.types, key=attrgetter("name")):
            if typ.key[:2] != (database, schema):
                continue

            yield TreeNode(
                path=(*steps, typ.name),
                label=typ.name,
                kind=TreeKind.OBJECT,
                children_count=0,
                detail=typ.kind.value,
                comment=typ.comment,
                ref=ObjectRef(source_id=source_id, kind=ObjectKind.TYPE, path=typ.key),
            )
