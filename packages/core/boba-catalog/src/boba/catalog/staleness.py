"""Устаревание процесса относительно новых версий снимков подключений: узел
указывает на удалённый или изменённый объект, поток именует колонку, которой
больше нет или у которой сменился тип.

Считается по diff между привязанной версией подключения и последней; ничего
не чинит, только называет причину для человека и LLM.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from uuid import UUID

from boba.catalog.base import CatalogModel, ChangeStatus
from boba.catalog.model import (
    CatalogSnapshot,
    EntityKind,
    EntityRef,
    Flow,
    FlowEnd,
    Node,
)
from boba.catalog.source_diff import ObjectChange, PartKind, SourceDiff, SourceSnapshot
from boba.catalog.sources import ObjectRef

__all__ = ["PinnedSnapshot", "Stale", "StaleReason", "Staleness"]

ChangeKey = tuple[str, tuple[str, ...]]


class StaleReason(StrEnum):
    OBJECT_REMOVED = "object_removed"
    OBJECT_CHANGED = "object_changed"
    COLUMN_REMOVED = "column_removed"
    COLUMN_CHANGED = "column_changed"


class PinnedSnapshot(CatalogModel):
    """Версия снимка подключения: привязанная или последняя."""

    version: int
    snapshot: SourceSnapshot


class Stale(CatalogModel):
    target: EntityRef
    connection_id: UUID
    pinned_version: int
    since_version: int
    reason: StaleReason
    detail: Mapping[str, str] = {}


class ConnectionGap(CatalogModel):
    """Разрыв между привязанной и последней версией одного подключения."""

    connection_id: UUID
    pinned_version: int
    since_version: int

    def stale(
        self, target: EntityRef, reason: StaleReason, detail: Mapping[str, str]
    ) -> Stale:
        return Stale(
            target=target,
            connection_id=self.connection_id,
            pinned_version=self.pinned_version,
            since_version=self.since_version,
            reason=reason,
            detail=detail,
        )


class Staleness(CatalogModel):
    """Список устареваний процесса; пустой — всё сходится."""

    entries: tuple[Stale, ...]

    @classmethod
    def compute(
        cls,
        process: CatalogSnapshot,
        pinned: Mapping[UUID, PinnedSnapshot],
        latest: Mapping[UUID, PinnedSnapshot],
    ) -> Staleness:
        entries: list[Stale] = []
        for connection_id, current in latest.items():
            base = pinned.get(connection_id)
            if base is None:
                continue

            if base.version == current.version:
                continue

            gap = ConnectionGap(
                connection_id=connection_id,
                pinned_version=base.version,
                since_version=current.version,
            )
            diff = SourceDiff.between(connection_id, base.snapshot, current.snapshot)
            entries.extend(cls._of_connection(process, gap, diff))

        return cls(entries=tuple(entries))

    def of_target(self, target: EntityRef) -> Iterator[Stale]:
        for entry in self.entries:
            if entry.target != target:
                continue

            yield entry

    @classmethod
    def _of_connection(
        cls, process: CatalogSnapshot, gap: ConnectionGap, diff: SourceDiff
    ) -> Iterator[Stale]:
        changes = cls._by_ref(diff)
        for node in process.nodes.values():
            if node.ref.connection_id != gap.connection_id:
                continue

            change = changes.get(cls._key(node.ref))
            if change is None:
                continue

            yield from cls._node_stale(node, change, gap)

        for flow in process.flows.values():
            yield from cls._flow_stale(process, flow, changes, gap)

    @staticmethod
    def _key(ref: ObjectRef) -> ChangeKey:
        return (ref.kind.value, ref.path)

    @classmethod
    def _by_ref(cls, diff: SourceDiff) -> dict[ChangeKey, ObjectChange]:
        by_ref: dict[ChangeKey, ObjectChange] = {}
        for entry in diff.entries:
            by_ref[cls._key(entry.ref)] = entry

        return by_ref

    @staticmethod
    def _node_stale(
        node: Node, change: ObjectChange, gap: ConnectionGap
    ) -> Iterator[Stale]:
        target = EntityRef(kind=EntityKind.NODE, id=node.id)
        if change.status is ChangeStatus.REMOVED:
            yield gap.stale(target, StaleReason.OBJECT_REMOVED, {})
            return

        if change.status is not ChangeStatus.MODIFIED:
            return

        detail: dict[str, str] = {}
        for field in change.fields:
            detail[field.field] = f"{field.was} -> {field.now}"

        for part in change.parts:
            detail[f"{part.part.value} {part.name}"] = part.status.value

        yield gap.stale(target, StaleReason.OBJECT_CHANGED, detail)

    @classmethod
    def _flow_stale(
        cls,
        process: CatalogSnapshot,
        flow: Flow,
        changes: Mapping[ChangeKey, ObjectChange],
        gap: ConnectionGap,
    ) -> Iterator[Stale]:
        target = EntityRef(kind=EntityKind.FLOW, id=flow.id)
        for end in FlowEnd:
            node = process.nodes.get(flow.node_at(end))
            if node is None:
                continue

            if node.ref.connection_id != gap.connection_id:
                continue

            change = changes.get(cls._key(node.ref))
            if change is None:
                continue

            for column in flow.columns_at(end):
                yield from cls._column_stale(target, change, gap, end, column)

    @staticmethod
    def _column_stale(
        target: EntityRef,
        change: ObjectChange,
        gap: ConnectionGap,
        end: FlowEnd,
        column: str,
    ) -> Iterator[Stale]:
        if change.status is ChangeStatus.REMOVED:
            detail = {"side": end.value, "column": column, "object": "removed"}
            yield gap.stale(target, StaleReason.COLUMN_REMOVED, detail)
            return

        for part in change.parts:
            if part.part is not PartKind.COLUMN:
                continue

            if part.name != column:
                continue

            if part.status is ChangeStatus.REMOVED:
                detail = {"side": end.value, "column": column}
                yield gap.stale(target, StaleReason.COLUMN_REMOVED, detail)
                continue

            if part.status is not ChangeStatus.MODIFIED:
                continue

            changed: dict[str, str] = {"side": end.value, "column": column}
            for item in part.fields:
                changed[item.field] = f"{item.was} -> {item.now}"

            yield gap.stale(target, StaleReason.COLUMN_CHANGED, changed)
