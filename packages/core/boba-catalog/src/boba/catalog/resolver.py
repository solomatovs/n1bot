"""Резолвер объектов по снимкам источников: реализация ObjectResolver над
привязанными версиями. Сервис собирает его из SourceStore, домен проверяет
им ссылки узлов и колонки в значениях потоков; страница получает через него
колонки узлов с типом и признаком ключа."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from uuid import UUID

from boba.catalog.base import CatalogModel
from boba.catalog.clickhouse_snapshot import ChSnapshot
from boba.catalog.model import ObjectResolver
from boba.catalog.postgres_snapshot import PgConstraintKind, PgSnapshot
from boba.catalog.source_diff import SourceSnapshot
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = ["NodeColumn", "SnapshotResolver"]


class NodeColumn(CatalogModel):
    """Колонка объекта глазами процесса: имя, тип, nullable и вхождение в
    первичный ключ. Общая форма для Postgres и ClickHouse, чтобы карточка
    узла рисовалась одинаково."""

    name: str
    type: str
    nullable: bool
    key: bool


class SnapshotResolver(ObjectResolver):
    """Объекты и колонки из снимков источников по их id; источник без снимка
    в наборе неизвестен: объекты считаются существующими, колонки — нет."""

    def __init__(self, snapshots: Mapping[UUID, SourceSnapshot]) -> None:
        self._snapshots = dict(snapshots)

    def known(self, source_id: UUID) -> bool:
        return source_id in self._snapshots

    def exists(self, ref: ObjectRef) -> bool:
        snapshot = self._snapshots.get(ref.source_id)
        if snapshot is None:
            return True

        if isinstance(snapshot, PgSnapshot):
            return self._pg_exists(snapshot, ref)

        return self._ch_exists(snapshot, ref)

    def columns_of(self, ref: ObjectRef) -> Sequence[str] | None:
        snapshot = self._snapshots.get(ref.source_id)
        if snapshot is None:
            return None

        if isinstance(snapshot, PgSnapshot):
            if ref.kind is not ObjectKind.RELATION:
                return None

            if snapshot.relation(ref.path) is None:
                return None

            names: list[str] = []
            for column in snapshot.columns_of(ref.path):
                names.append(column.name)

            return names

        return self._ch_columns(snapshot, ref)

    def node_columns(self, ref: ObjectRef) -> tuple[NodeColumn, ...]:
        """Колонки объекта для карточки узла; у объектов без колонок и у
        неизвестного источника пусто."""
        snapshot = self._snapshots.get(ref.source_id)
        if snapshot is None:
            return ()

        if isinstance(snapshot, PgSnapshot):
            return tuple(self._pg_node_columns(snapshot, ref))

        return tuple(self._ch_node_columns(snapshot, ref))

    @staticmethod
    def _pg_node_columns(snapshot: PgSnapshot, ref: ObjectRef) -> Iterator[NodeColumn]:
        if ref.kind is not ObjectKind.RELATION:
            return

        keys: set[str] = set()
        for constraint in snapshot.constraints_of(ref.path):
            if constraint.kind is not PgConstraintKind.PRIMARY:
                continue

            keys.update(constraint.columns)

        for column in snapshot.columns_of(ref.path):
            yield NodeColumn(
                name=column.name,
                type=column.type,
                nullable=column.nullable,
                key=column.name in keys,
            )

    @staticmethod
    def _ch_node_columns(snapshot: ChSnapshot, ref: ObjectRef) -> Iterator[NodeColumn]:
        if ref.kind is not ObjectKind.TABLE:
            return

        for column in snapshot.columns_of(ref.path):
            yield NodeColumn(
                name=column.name,
                type=column.type,
                nullable=column.type.startswith("Nullable("),
                key=column.in_primary_key,
            )

    @staticmethod
    def _pg_exists(snapshot: PgSnapshot, ref: ObjectRef) -> bool:
        if ref.kind is ObjectKind.RELATION:
            return snapshot.relation(ref.path) is not None

        if ref.kind is ObjectKind.ROUTINE:
            return snapshot.routine(ref.path) is not None

        if ref.kind is ObjectKind.SEQUENCE:
            return snapshot.sequence(ref.path) is not None

        if ref.kind is ObjectKind.TYPE:
            return snapshot.type(ref.path) is not None

        return False

    @staticmethod
    def _ch_exists(snapshot: ChSnapshot, ref: ObjectRef) -> bool:
        if ref.kind is ObjectKind.TABLE:
            return snapshot.table(ref.path) is not None

        if ref.kind is ObjectKind.DICTIONARY:
            return snapshot.dictionary(ref.path) is not None

        return False

    @staticmethod
    def _ch_columns(snapshot: ChSnapshot, ref: ObjectRef) -> Sequence[str] | None:
        if ref.kind is not ObjectKind.TABLE:
            return None

        if snapshot.table(ref.path) is None:
            return None

        names: list[str] = []
        for column in snapshot.columns_of(ref.path):
            names.append(column.name)

        return names
