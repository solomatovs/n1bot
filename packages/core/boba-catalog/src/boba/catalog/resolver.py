"""Резолвер объектов по снимкам подключений: реализация ObjectResolver над
привязанными версиями. Сервис собирает его из хранилища версий, домен
проверяет им ссылки узлов и пары колонок потоков; страница получает через
него колонки узлов. Вопросы о снимке снимок решает сам — здесь только выбор
снимка по id подключения."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from boba.catalog.model import ObjectResolver
from boba.catalog.sources import NodeColumn, ObjectRef, SourceSnapshot

__all__ = ["SnapshotResolver"]


class SnapshotResolver(ObjectResolver):
    """Объекты и колонки из снимков подключений по их id; подключение без
    снимка в наборе неизвестно: объекты считаются существующими, колонки — нет."""

    def __init__(self, snapshots: Mapping[UUID, SourceSnapshot]) -> None:
        self._snapshots = dict(snapshots)

    def known(self, connection_id: UUID) -> bool:
        return connection_id in self._snapshots

    def exists(self, ref: ObjectRef) -> bool:
        snapshot = self._snapshots.get(ref.connection_id)
        if snapshot is None:
            return True

        return snapshot.exists(ref)

    def columns_of(self, ref: ObjectRef) -> Sequence[str] | None:
        snapshot = self._snapshots.get(ref.connection_id)
        if snapshot is None:
            return None

        return snapshot.column_names(ref)

    def node_columns(self, ref: ObjectRef) -> tuple[NodeColumn, ...]:
        """Колонки объекта для карточки узла; у неизвестного подключения пусто."""
        snapshot = self._snapshots.get(ref.connection_id)
        if snapshot is None:
            return ()

        return snapshot.node_columns(ref)
