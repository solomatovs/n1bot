"""Резолвер объектов по снимкам источников: реализация ObjectResolver над
привязанными версиями. Сервис собирает его из SourceStore, домен проверяет
им ссылки узлов и колонки в значениях потоков."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from boba.catalog.clickhouse_snapshot import ChSnapshot
from boba.catalog.model import ObjectResolver
from boba.catalog.postgres_snapshot import PgSnapshot
from boba.catalog.source_diff import SourceSnapshot
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = ["SnapshotResolver"]


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
