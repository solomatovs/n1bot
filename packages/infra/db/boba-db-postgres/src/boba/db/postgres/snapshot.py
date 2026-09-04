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

from collections.abc import Iterator, Mapping, Sequence
from enum import IntEnum, StrEnum
from operator import attrgetter
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field

from boba.catalog.base import CatalogError, CatalogInvariantError, CatalogModel
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

    def card(self, snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
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


class PgPathDepth(IntEnum):
    """Длина пути объекта ручного источника: база, схема, имя."""

    OBJECT = 3


class PgSnapshot(SourceSnapshot):
    """Снимок Postgres одной версии: плоские таблицы записей.

    Реализация SourceSnapshot: части и семейства объявлены, инварианты и
    поиск по адресу даёт база; родное здесь — дерево (база → схема → группа
    → объект, у секционированной таблицы дети — секции), карточки, колонки
    узла с первичным ключом из ограничений и объекты ручного источника.
    """

    TABLE_PREFIX: ClassVar[str] = "pg"
    SYNC_TOOL: ClassVar[str] = "pg_schema_snapshot"
    MANUAL_KIND: ClassVar[ObjectKind] = ObjectKind.RELATION
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
        for constraint in self.constraints_of(ref.path):
            if constraint.kind is not PgConstraintKind.PRIMARY:
                continue

            keys.update(constraint.columns)

        columns: list[NodeColumn] = []
        for column in self.columns_of(ref.path):
            columns.append(
                NodeColumn(
                    name=column.name,
                    type=column.type,
                    nullable=column.nullable,
                    key=column.name in keys,
                )
            )

        return tuple(columns)

    def with_object(self, obj: ManualObject) -> PgSnapshot:
        if len(obj.path) != PgPathDepth.OBJECT:
            rendered = "/".join(obj.path)
            msg = f"postgres object path must be database/schema/name: {rendered}"
            raise CatalogInvariantError([msg])

        database, schema, name = obj.path
        databases = self.databases
        if (database,) not in Keyed.keys_of(self.databases):
            databases = (*databases, PgDatabase(name=database))

        schemas = self.schemas
        if (database, schema) not in Keyed.keys_of(self.schemas):
            schemas = (*schemas, PgSchema(database=database, name=schema))

        kind = PgRelationKind.TABLE
        if obj.kind is ManualObjectKind.VIEW:
            kind = PgRelationKind.VIEW

        relation = PgRelation(
            database=database,
            schema_name=schema,
            name=name,
            kind=kind,
            comment=obj.comment,
        )
        columns = list(self.columns)
        for ordinal, column in enumerate(obj.columns, start=1):
            columns.append(
                PgColumn(
                    database=database,
                    schema_name=schema,
                    relation=name,
                    name=column.name,
                    ordinal=ordinal,
                    type=column.type,
                    nullable=column.nullable,
                    comment=column.comment,
                )
            )

        return self.model_copy(
            update={
                "databases": databases,
                "schemas": schemas,
                "relations": (*self.relations, relation),
                "columns": tuple(columns),
            }
        )

    def without_object(self, path: Sequence[str]) -> PgSnapshot:
        wanted = tuple(path)
        return self.model_copy(
            update={
                "relations": Records.without_key(self.relations, wanted),
                "columns": Records.without_parent(self.columns, wanted),
                "constraints": Records.without_parent(self.constraints, wanted),
                "indexes": Records.without_parent(self.indexes, wanted),
            }
        )

    def manual_object(self, ref: ObjectRef) -> ManualObject:
        found = self.require_object(ref)
        if not isinstance(found, PgRelation):
            msg = f"{ref.kind.value} at {ref.render()} is not a manual object"
            raise CatalogError(msg)

        kind = ManualObjectKind.TABLE
        if found.kind is PgRelationKind.VIEW:
            kind = ManualObjectKind.VIEW

        columns: list[ManualColumn] = []
        for column in self.columns_of(ref.path):
            columns.append(
                ManualColumn(
                    name=column.name,
                    type=column.type,
                    nullable=column.nullable,
                    comment=column.comment,
                )
            )

        return ManualObject(
            kind=kind, path=found.key, comment=found.comment, columns=tuple(columns)
        )

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
        parent = self.relation(path)
        if parent is None:
            return

        for relation in self.relations:
            if relation.kind is not PgRelationKind.PARTITION:
                continue

            if relation.database != parent.database:
                continue

            if relation.partition_of != parent.partition_label:
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
