"""Операции ручного источника: объект с коротким набором полей (имя, вид,
комментарий, колонки с типом, nullable и комментарием) добавляется, заменяется
или удаляется в снимке родной структуры. База и схема из пути заводятся сами.

Ошибки:
SourceOpError — операция не применима: объекта нет, объект уже есть, путь
    не той длины для вида источника.
CatalogInvariantError — снимок после операции нарушает инварианты.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import Field, RootModel

from boba.catalog.base import CatalogError, CatalogInvariantError, CatalogModel
from boba.catalog.clickhouse_snapshot import (
    ChColumn,
    ChDatabase,
    ChSnapshot,
    ChTable,
    ChTableKind,
)
from boba.catalog.postgres_snapshot import (
    PgColumn,
    PgDatabase,
    PgRelation,
    PgRelationKind,
    PgSchema,
    PgSnapshot,
)
from boba.catalog.source_diff import SourceSnapshot
from boba.catalog.sources import SourceRecord

RecordT = TypeVar("RecordT", bound=SourceRecord)

__all__ = [
    "AddObject",
    "ManualColumn",
    "ManualObject",
    "ManualObjectKind",
    "ManualObjects",
    "RemoveObject",
    "SetObject",
    "SourceOp",
    "SourceOpError",
    "SourceOpKind",
    "SourceOperationList",
]


class ManualObjectKind(StrEnum):
    TABLE = "table"
    VIEW = "view"


class SourceOpKind(StrEnum):
    ADD_OBJECT = "add_object"
    SET_OBJECT = "set_object"
    REMOVE_OBJECT = "remove_object"


class ManualColumn(CatalogModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    nullable: bool = True
    comment: str | None = None


class ManualObject(CatalogModel):
    """Объект ручного источника: путь как у реального (Postgres: база, схема,
    имя; ClickHouse: база, имя), вид, комментарий, колонки по порядку."""

    kind: ManualObjectKind = ManualObjectKind.TABLE
    path: tuple[str, ...] = Field(min_length=2)
    comment: str | None = None
    columns: tuple[ManualColumn, ...] = ()


class SourceOpError(CatalogError):
    """Операция из списка не применима; index — её номер в списке."""

    def __init__(self, index: int, op: SourceOpBase, reason: str) -> None:
        self.index = index
        self.op = op
        self.reason = reason
        super().__init__(f"operation #{index} ({op.op.value}): {reason}")


class SourceOpBase(CatalogModel, ABC):
    op: SourceOpKind

    @abstractmethod
    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        """Новый снимок с применённой операцией.

        Ошибки:
        CatalogInvariantError — операция не применима; причина в сообщении.
        """


class AddObject(SourceOpBase):
    op: Literal[SourceOpKind.ADD_OBJECT] = SourceOpKind.ADD_OBJECT
    object: ManualObject

    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if ManualObjects.exists(snapshot, self.object.path):
            msg = f"object {'/'.join(self.object.path)} already exists"
            raise CatalogInvariantError([msg])

        return ManualObjects.put(snapshot, self.object)


class SetObject(SourceOpBase):
    op: Literal[SourceOpKind.SET_OBJECT] = SourceOpKind.SET_OBJECT
    object: ManualObject

    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if not ManualObjects.exists(snapshot, self.object.path):
            msg = f"object {'/'.join(self.object.path)} not found"
            raise CatalogInvariantError([msg])

        without = ManualObjects.drop(snapshot, self.object.path)
        return ManualObjects.put(without, self.object)


class RemoveObject(SourceOpBase):
    op: Literal[SourceOpKind.REMOVE_OBJECT] = SourceOpKind.REMOVE_OBJECT
    path: tuple[str, ...] = Field(min_length=2)

    def apply_to(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        if not ManualObjects.exists(snapshot, self.path):
            msg = f"object {'/'.join(self.path)} not found"
            raise CatalogInvariantError([msg])

        return ManualObjects.drop(snapshot, self.path)


SourceOp = AddObject | SetObject | RemoveObject


class SourceOperationList(RootModel[tuple[SourceOp, ...]]):
    """Список операций ручного источника: разбор JSON на границе и применение
    к снимку с проверкой инвариантов после каждой операции."""

    def apply(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        """Ошибки:
        SourceOpError — первая не применимая операция, дальше не идём.
        """
        current = snapshot
        for index, op in enumerate(self.root):
            try:
                current = op.apply_to(current)
                current.check()
            except CatalogInvariantError as exc:
                raise SourceOpError(index, op, str(exc)) from exc

        return current


class PgPathDepth:
    """Длина пути объекта Postgres: база, схема, имя."""

    OBJECT = 3


class ChPathDepth:
    """Длина пути объекта ClickHouse: база, имя."""

    OBJECT = 2


class ManualObjects:
    """Перевод короткого описания объекта в записи снимка родной структуры и
    обратно; база и схема заводятся по пути, если их ещё нет."""

    @staticmethod
    def exists(snapshot: SourceSnapshot, path: Sequence[str]) -> bool:
        if isinstance(snapshot, PgSnapshot):
            return snapshot.relation(path) is not None

        return snapshot.table(path) is not None

    @staticmethod
    def put(snapshot: SourceSnapshot, obj: ManualObject) -> SourceSnapshot:
        if isinstance(snapshot, PgSnapshot):
            return ManualObjects._put_pg(snapshot, obj)

        return ManualObjects._put_ch(snapshot, obj)

    @staticmethod
    def drop(snapshot: SourceSnapshot, path: Sequence[str]) -> SourceSnapshot:
        wanted = tuple(path)
        if isinstance(snapshot, PgSnapshot):
            return snapshot.model_copy(
                update={
                    "relations": ManualObjects._without_key(snapshot.relations, wanted),
                    "columns": ManualObjects._without_parent(snapshot.columns, wanted),
                    "constraints": ManualObjects._without_parent(
                        snapshot.constraints, wanted
                    ),
                    "indexes": ManualObjects._without_parent(snapshot.indexes, wanted),
                }
            )

        return snapshot.model_copy(
            update={
                "tables": ManualObjects._without_key(snapshot.tables, wanted),
                "columns": ManualObjects._without_parent(snapshot.columns, wanted),
            }
        )

    @staticmethod
    def _without_key(
        records: Sequence[RecordT], key: tuple[str, ...]
    ) -> tuple[RecordT, ...]:
        kept: list[RecordT] = []
        for record in records:
            if record.key == key:
                continue

            kept.append(record)

        return tuple(kept)

    @staticmethod
    def _without_parent(
        records: Sequence[RecordT], parent: tuple[str, ...]
    ) -> tuple[RecordT, ...]:
        kept: list[RecordT] = []
        for record in records:
            if record.parent == parent:
                continue

            kept.append(record)

        return tuple(kept)

    @staticmethod
    def _put_pg(snapshot: PgSnapshot, obj: ManualObject) -> PgSnapshot:
        if len(obj.path) != PgPathDepth.OBJECT:
            rendered = "/".join(obj.path)
            msg = f"postgres object path must be database/schema/name: {rendered}"
            raise CatalogInvariantError([msg])

        database, schema, name = obj.path
        databases = snapshot.databases
        if ManualObjects._missing(snapshot.databases, (database,)):
            databases = (*databases, PgDatabase(name=database))

        schemas = snapshot.schemas
        if ManualObjects._missing(snapshot.schemas, (database, schema)):
            schemas = (*schemas, PgSchema(database=database, name=schema))

        relation = PgRelation(
            database=database,
            schema_name=schema,
            name=name,
            kind=ManualObjects._pg_kind(obj.kind),
            comment=obj.comment,
        )
        columns = list(snapshot.columns)
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

        return snapshot.model_copy(
            update={
                "databases": databases,
                "schemas": schemas,
                "relations": (*snapshot.relations, relation),
                "columns": tuple(columns),
            }
        )

    @staticmethod
    def _put_ch(snapshot: ChSnapshot, obj: ManualObject) -> ChSnapshot:
        if len(obj.path) != ChPathDepth.OBJECT:
            msg = f"clickhouse object path must be database/name: {'/'.join(obj.path)}"
            raise CatalogInvariantError([msg])

        database, name = obj.path
        databases = snapshot.databases
        if ManualObjects._missing(snapshot.databases, (database,)):
            databases = (*databases, ChDatabase(name=database))

        table = ChTable(
            database=database,
            name=name,
            kind=ManualObjects._ch_kind(obj.kind),
            comment=obj.comment,
        )
        columns = list(snapshot.columns)
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

        return snapshot.model_copy(
            update={
                "databases": databases,
                "tables": (*snapshot.tables, table),
                "columns": tuple(columns),
            }
        )

    @staticmethod
    def _pg_kind(kind: ManualObjectKind) -> PgRelationKind:
        if kind is ManualObjectKind.VIEW:
            return PgRelationKind.VIEW

        return PgRelationKind.TABLE

    @staticmethod
    def _ch_kind(kind: ManualObjectKind) -> ChTableKind:
        if kind is ManualObjectKind.VIEW:
            return ChTableKind.VIEW

        return ChTableKind.TABLE

    @staticmethod
    def of_relation(snapshot: PgSnapshot, relation: PgRelation) -> ManualObject:
        """Короткое описание существующего объекта: для формы правки."""
        columns = list(ManualObjects._pg_columns(snapshot, relation))
        kind = ManualObjectKind.TABLE
        if relation.kind is PgRelationKind.VIEW:
            kind = ManualObjectKind.VIEW

        return ManualObject(
            kind=kind,
            path=relation.key,
            comment=relation.comment,
            columns=tuple(columns),
        )

    @staticmethod
    def _pg_columns(
        snapshot: PgSnapshot, relation: PgRelation
    ) -> Iterator[ManualColumn]:
        for column in snapshot.columns_of(relation.key):
            yield ManualColumn(
                name=column.name,
                type=column.type,
                nullable=column.nullable,
                comment=column.comment,
            )

    @staticmethod
    def _missing(records: Sequence[SourceRecord], key: tuple[str, ...]) -> bool:
        known: set[tuple[str, ...]] = set()
        for record in records:
            known.add(record.key)

        return key not in known
