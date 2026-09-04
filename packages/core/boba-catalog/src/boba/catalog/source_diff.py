"""Разница двух версий снимка источника по адресам объектов и по полям:
какие объекты появились, исчезли, изменились; у изменённых — какие поля и
какие части (колонки, ограничения, индексы, аргументы, атрибуты).

Ошибки:
CatalogError — снимки разного вида сравнить нельзя.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any
from uuid import UUID

from boba.catalog.clickhouse_snapshot import ChSnapshot
from boba.catalog.diff import ChangeStatus
from boba.catalog.model import CatalogError, CatalogModel
from boba.catalog.postgres_snapshot import PgSnapshot
from boba.catalog.sources import ObjectKind, ObjectRef, SourceRecord

__all__ = [
    "FieldChange",
    "ObjectChange",
    "PartChange",
    "PartKind",
    "SourceDiff",
    "SourceSnapshot",
]

SourceSnapshot = PgSnapshot | ChSnapshot


class PartKind(StrEnum):
    """Что за часть объекта изменилась."""

    COLUMN = "column"
    CONSTRAINT = "constraint"
    INDEX = "index"
    ARGUMENT = "argument"
    ATTRIBUTE = "attribute"


class FieldChange(CatalogModel):
    """Поле, значение которого изменилось; значения отданы строкой для показа."""

    field: str
    was: str | None
    now: str | None


class PartChange(CatalogModel):
    part: PartKind
    name: str
    status: ChangeStatus
    fields: tuple[FieldChange, ...] = ()


class ObjectChange(CatalogModel):
    ref: ObjectRef
    label: str
    status: ChangeStatus
    fields: tuple[FieldChange, ...] = ()
    parts: tuple[PartChange, ...] = ()


class PartTable(CatalogModel):
    part: PartKind
    old: tuple[SourceRecord, ...]
    new: tuple[SourceRecord, ...]


class RecordTable(CatalogModel):
    """Таблица записей одного рода в обоих снимках: что сравнивать и как звать."""

    kind: ObjectKind
    old: tuple[SourceRecord, ...]
    new: tuple[SourceRecord, ...]
    parts: tuple[PartTable, ...] = ()


class SourceDiff(CatalogModel):
    """Список изменений между двумя версиями одного источника.

    Считается по адресам: объект ищется в обеих версиях по ключу, поля
    сравниваются по значению. Базы и схемы тоже входят в список, чтобы дерево
    могло пометить контейнеры.
    """

    entries: tuple[ObjectChange, ...]

    @classmethod
    def between(
        cls, source_id: UUID, old: SourceSnapshot, new: SourceSnapshot
    ) -> SourceDiff:
        """Ошибки:
        CatalogError — снимки разных видов.
        """
        if old.kind is not new.kind:
            msg = f"cannot diff {old.kind.value} against {new.kind.value}"
            raise CatalogError(msg)

        tables = list(cls._tables(old, new))
        entries: list[ObjectChange] = []
        for table in tables:
            entries.extend(cls._table_changes(source_id, table))

        return cls(entries=tuple(entries))

    def status_of(self, ref: ObjectRef) -> ChangeStatus:
        for entry in self.entries:
            if entry.ref.kind is not ref.kind:
                continue

            if entry.ref.path != ref.path:
                continue

            return entry.status

        return ChangeStatus.UNCHANGED

    def touched_prefixes(self) -> set[tuple[str, ...]]:
        """Все префиксы путей изменённых объектов: по ним дерево помечает
        родителей как изменённые."""
        prefixes: set[tuple[str, ...]] = set()
        for entry in self.entries:
            path = entry.ref.path
            for depth in range(1, len(path)):
                prefixes.add(path[:depth])

        return prefixes

    @staticmethod
    def _tables(old: SourceSnapshot, new: SourceSnapshot) -> Iterator[RecordTable]:
        if isinstance(old, PgSnapshot) and isinstance(new, PgSnapshot):
            yield RecordTable(
                kind=ObjectKind.DATABASE, old=old.databases, new=new.databases
            )
            yield RecordTable(kind=ObjectKind.SCHEMA, old=old.schemas, new=new.schemas)
            yield RecordTable(
                kind=ObjectKind.RELATION,
                old=old.relations,
                new=new.relations,
                parts=(
                    PartTable(part=PartKind.COLUMN, old=old.columns, new=new.columns),
                    PartTable(
                        part=PartKind.CONSTRAINT,
                        old=old.constraints,
                        new=new.constraints,
                    ),
                    PartTable(part=PartKind.INDEX, old=old.indexes, new=new.indexes),
                ),
            )
            yield RecordTable(
                kind=ObjectKind.ROUTINE,
                old=old.routines,
                new=new.routines,
                parts=(
                    PartTable(
                        part=PartKind.ARGUMENT,
                        old=old.routine_args,
                        new=new.routine_args,
                    ),
                ),
            )
            yield RecordTable(
                kind=ObjectKind.SEQUENCE, old=old.sequences, new=new.sequences
            )
            yield RecordTable(kind=ObjectKind.TYPE, old=old.types, new=new.types)
            return

        if isinstance(old, ChSnapshot) and isinstance(new, ChSnapshot):
            yield RecordTable(
                kind=ObjectKind.DATABASE, old=old.databases, new=new.databases
            )
            yield RecordTable(
                kind=ObjectKind.TABLE,
                old=old.tables,
                new=new.tables,
                parts=(
                    PartTable(part=PartKind.COLUMN, old=old.columns, new=new.columns),
                ),
            )
            yield RecordTable(
                kind=ObjectKind.DICTIONARY,
                old=old.dictionaries,
                new=new.dictionaries,
                parts=(
                    PartTable(
                        part=PartKind.ATTRIBUTE,
                        old=old.dictionary_attributes,
                        new=new.dictionary_attributes,
                    ),
                ),
            )

    @classmethod
    def _table_changes(
        cls, source_id: UUID, table: RecordTable
    ) -> Iterator[ObjectChange]:
        old = cls._by_key(table.old)
        new = cls._by_key(table.new)
        for key in sorted(old.keys() | new.keys()):
            ref = ObjectRef(source_id=source_id, kind=table.kind, path=key)
            before = old.get(key)
            after = new.get(key)
            if before is None and after is not None:
                yield ObjectChange(
                    ref=ref, label=cls._label(after), status=ChangeStatus.ADDED
                )
                continue

            if after is None and before is not None:
                yield ObjectChange(
                    ref=ref, label=cls._label(before), status=ChangeStatus.REMOVED
                )
                continue

            if before is None or after is None:
                continue

            fields = tuple(cls._field_changes(before, after))
            parts = tuple(cls._part_changes(key, table.parts))
            if not fields and not parts:
                continue

            yield ObjectChange(
                ref=ref,
                label=cls._label(after),
                status=ChangeStatus.MODIFIED,
                fields=fields,
                parts=parts,
            )

    @classmethod
    def _part_changes(
        cls, parent: tuple[str, ...], parts: Sequence[PartTable]
    ) -> Iterator[PartChange]:
        for part in parts:
            old = cls._by_key(cls._children(part.old, parent))
            new = cls._by_key(cls._children(part.new, parent))
            for key in sorted(old.keys() | new.keys()):
                name = key[-1]
                before = old.get(key)
                after = new.get(key)
                if before is None and after is not None:
                    yield PartChange(
                        part=part.part, name=name, status=ChangeStatus.ADDED
                    )
                    continue

                if after is None and before is not None:
                    yield PartChange(
                        part=part.part, name=name, status=ChangeStatus.REMOVED
                    )
                    continue

                if before is None or after is None:
                    continue

                fields = tuple(cls._field_changes(before, after))
                if not fields:
                    continue

                yield PartChange(
                    part=part.part,
                    name=name,
                    status=ChangeStatus.MODIFIED,
                    fields=fields,
                )

    @staticmethod
    def _children(
        records: Iterable[SourceRecord], parent: tuple[str, ...]
    ) -> Iterator[SourceRecord]:
        for record in records:
            if record.parent != parent:
                continue

            yield record

    @staticmethod
    def _by_key(records: Iterable[SourceRecord]) -> dict[tuple[str, ...], SourceRecord]:
        by_key: dict[tuple[str, ...], SourceRecord] = {}
        for record in records:
            by_key[record.key] = record

        return by_key

    @staticmethod
    def _field_changes(
        before: SourceRecord, after: SourceRecord
    ) -> Iterator[FieldChange]:
        was: Mapping[str, Any] = before.model_dump(mode="json")
        now: Mapping[str, Any] = after.model_dump(mode="json")
        for field in was:
            if was[field] == now.get(field):
                continue

            yield FieldChange(
                field=field,
                was=SourceDiff._render(was[field]),
                now=SourceDiff._render(now.get(field)),
            )

    @staticmethod
    def _render(value: object) -> str | None:
        if value is None:
            return None

        if isinstance(value, str):
            return value

        return str(value)

    @staticmethod
    def _label(record: SourceRecord) -> str:
        return record.label
