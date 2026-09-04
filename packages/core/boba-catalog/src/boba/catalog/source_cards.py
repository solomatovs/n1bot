"""Карточка объекта источника для панели деталей: сама запись и её части
(колонки, ограничения, индексы, секции, аргументы, атрибуты) из снимка одной
версии. Форма родная: у Postgres и ClickHouse свои карточки.

Ошибки:
CatalogError — по адресу в снимке нет объекта такого вида.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from boba.catalog.clickhouse_snapshot import (
    ChColumn,
    ChDictionary,
    ChDictionaryAttribute,
    ChSnapshot,
    ChTable,
)
from boba.catalog.model import CatalogError, CatalogModel
from boba.catalog.postgres_snapshot import (
    PgColumn,
    PgConstraint,
    PgIndex,
    PgRelation,
    PgRoutine,
    PgRoutineArg,
    PgSequence,
    PgSnapshot,
    PgType,
)
from boba.catalog.source_diff import SourceSnapshot
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = [
    "CardKind",
    "ChDictionaryCard",
    "ChTableCard",
    "ObjectCard",
    "ObjectCards",
    "PgRelationCard",
    "PgRoutineCard",
    "PgSequenceCard",
    "PgTypeCard",
]


class CardKind(StrEnum):
    """Дискриминаторы карточек."""

    PG_RELATION = "pg_relation"
    PG_ROUTINE = "pg_routine"
    PG_SEQUENCE = "pg_sequence"
    PG_TYPE = "pg_type"
    CH_TABLE = "ch_table"
    CH_DICTIONARY = "ch_dictionary"


class PgRelationCard(CatalogModel):
    card: Literal[CardKind.PG_RELATION] = CardKind.PG_RELATION
    ref: ObjectRef
    relation: PgRelation
    columns: tuple[PgColumn, ...]
    constraints: tuple[PgConstraint, ...]
    indexes: tuple[PgIndex, ...]
    partitions: tuple[PgRelation, ...]


class PgRoutineCard(CatalogModel):
    card: Literal[CardKind.PG_ROUTINE] = CardKind.PG_ROUTINE
    ref: ObjectRef
    routine: PgRoutine
    arguments: tuple[PgRoutineArg, ...]


class PgSequenceCard(CatalogModel):
    card: Literal[CardKind.PG_SEQUENCE] = CardKind.PG_SEQUENCE
    ref: ObjectRef
    sequence: PgSequence


class PgTypeCard(CatalogModel):
    card: Literal[CardKind.PG_TYPE] = CardKind.PG_TYPE
    ref: ObjectRef
    type: PgType


class ChTableCard(CatalogModel):
    card: Literal[CardKind.CH_TABLE] = CardKind.CH_TABLE
    ref: ObjectRef
    table: ChTable
    columns: tuple[ChColumn, ...]


class ChDictionaryCard(CatalogModel):
    card: Literal[CardKind.CH_DICTIONARY] = CardKind.CH_DICTIONARY
    ref: ObjectRef
    dictionary: ChDictionary
    attributes: tuple[ChDictionaryAttribute, ...]


ObjectCard = (
    PgRelationCard
    | PgRoutineCard
    | PgSequenceCard
    | PgTypeCard
    | ChTableCard
    | ChDictionaryCard
)


class ObjectCards:
    """Сборка карточки по адресу из снимка."""

    @staticmethod
    def of(snapshot: SourceSnapshot, ref: ObjectRef) -> ObjectCard:
        """Ошибки:
        CatalogError — объекта нет или вид не подходит снимку.
        """
        if isinstance(snapshot, PgSnapshot):
            return ObjectCards._postgres(snapshot, ref)

        return ObjectCards._clickhouse(snapshot, ref)

    @staticmethod
    def _postgres(snapshot: PgSnapshot, ref: ObjectRef) -> ObjectCard:
        if ref.kind is ObjectKind.RELATION:
            relation = snapshot.relation(ref.path)
            if relation is None:
                raise ObjectCards._missing(ref)

            return PgRelationCard(
                ref=ref,
                relation=relation,
                columns=tuple(snapshot.columns_of(ref.path)),
                constraints=tuple(snapshot.constraints_of(ref.path)),
                indexes=tuple(snapshot.indexes_of(ref.path)),
                partitions=tuple(snapshot.partitions_of(ref.path)),
            )

        if ref.kind is ObjectKind.ROUTINE:
            routine = snapshot.routine(ref.path)
            if routine is None:
                raise ObjectCards._missing(ref)

            return PgRoutineCard(
                ref=ref, routine=routine, arguments=tuple(snapshot.args_of(ref.path))
            )

        if ref.kind is ObjectKind.SEQUENCE:
            sequence = snapshot.sequence(ref.path)
            if sequence is None:
                raise ObjectCards._missing(ref)

            return PgSequenceCard(ref=ref, sequence=sequence)

        if ref.kind is ObjectKind.TYPE:
            typ = snapshot.type(ref.path)
            if typ is None:
                raise ObjectCards._missing(ref)

            return PgTypeCard(ref=ref, type=typ)

        raise ObjectCards._missing(ref)

    @staticmethod
    def _clickhouse(snapshot: ChSnapshot, ref: ObjectRef) -> ObjectCard:
        if ref.kind is ObjectKind.TABLE:
            table = snapshot.table(ref.path)
            if table is None:
                raise ObjectCards._missing(ref)

            return ChTableCard(
                ref=ref, table=table, columns=tuple(snapshot.columns_of(ref.path))
            )

        if ref.kind is ObjectKind.DICTIONARY:
            dictionary = snapshot.dictionary(ref.path)
            if dictionary is None:
                raise ObjectCards._missing(ref)

            return ChDictionaryCard(
                ref=ref,
                dictionary=dictionary,
                attributes=tuple(snapshot.attributes_of(ref.path)),
            )

        raise ObjectCards._missing(ref)

    @staticmethod
    def _missing(ref: ObjectRef) -> CatalogError:
        return CatalogError(f"no {ref.kind.value} at {ref.render()}")
