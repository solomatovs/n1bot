"""Совместимость процесса с новой версией снимка подключения — то, что
мешает перевести процесс на неё: узел указывает на удалённый объект, у
объекта пропала колонка или тип колонки перестал принимать прежние значения,
поток именует колонку пары, которой больше нет или тип которой сужен.
Добавленные колонки, расширение типов (varchar(30) → varchar(500)) и правки
самого объекта (тело процедуры, комментарий) процессу не мешают.

Считается по diff между привязанной версией подключения и новой; ничего не
чинит, только называет причину для человека и LLM. Пусто — переводить можно.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from operator import attrgetter
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
from boba.catalog.source_diff import (
    ObjectChange,
    PartChange,
    PartKind,
    SourceDiff,
    SourceSnapshot,
)
from boba.catalog.sources import ObjectRef

__all__ = ["PinnedSnapshot", "Stale", "StaleReason", "Staleness"]

ChangeKey = tuple[str, tuple[str, ...]]
TypeWidens = Callable[[str, str], bool]


class StaleReason(StrEnum):
    OBJECT_REMOVED = "object_removed"
    COLUMN_REMOVED = "column_removed"
    COLUMN_CHANGED = "column_changed"


class StaleDetail(StrEnum):
    """Ключи деталей причины."""

    COLUMN = "column"
    SIDE = "side"
    TYPE = "type"


class PinnedSnapshot(CatalogModel):
    """Версия снимка подключения: привязанная или новая."""

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
    """Разрыв между привязанной и новой версией одного подключения."""

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


class ColumnBreak(CatalogModel):
    """Колонка объекта, которая мешает переводу: пропала или тип сужен."""

    column: str
    reason: StaleReason
    type_change: str = ""

    def detail(self, side: FlowEnd | None = None) -> dict[str, str]:
        detail: dict[str, str] = {StaleDetail.COLUMN.value: self.column}
        if side is not None:
            detail[StaleDetail.SIDE.value] = side.value

        if self.type_change != "":
            detail[StaleDetail.TYPE.value] = self.type_change

        return detail


class Staleness(CatalogModel):
    """Список того, что мешает процессу перейти на новые версии; пустой —
    всё сходится."""

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
            widens = type(current.snapshot).type_widens
            entries.extend(cls._of_connection(process, gap, diff, widens))

        return cls(entries=tuple(entries))

    def of_target(self, target: EntityRef) -> Iterator[Stale]:
        for entry in self.entries:
            if entry.target != target:
                continue

            yield entry

    @classmethod
    def _of_connection(
        cls,
        process: CatalogSnapshot,
        gap: ConnectionGap,
        diff: SourceDiff,
        widens: TypeWidens,
    ) -> Iterator[Stale]:
        changes = cls._by_ref(diff)
        breaks: dict[UUID, dict[str, ColumnBreak]] = {}
        for node in process.nodes.values():
            if node.ref.connection_id != gap.connection_id:
                continue

            change = changes.get(cls._key(node.ref))
            if change is None:
                continue

            if change.status is ChangeStatus.REMOVED:
                target = EntityRef(kind=EntityKind.NODE, id=node.id)
                yield gap.stale(target, StaleReason.OBJECT_REMOVED, {})
                continue

            broken = cls._column_breaks(change, widens)
            breaks[node.id] = broken
            yield from cls._node_stale(node, broken, gap)

        for flow in process.flows.values():
            yield from cls._flow_stale(process, flow, changes, breaks, gap)

    @staticmethod
    def _key(ref: ObjectRef) -> ChangeKey:
        return (ref.kind.value, ref.path)

    @classmethod
    def _by_ref(cls, diff: SourceDiff) -> dict[ChangeKey, ObjectChange]:
        by_ref: dict[ChangeKey, ObjectChange] = {}
        for entry in diff.entries:
            by_ref[cls._key(entry.ref)] = entry

        return by_ref

    @classmethod
    def _column_breaks(
        cls, change: ObjectChange, widens: TypeWidens
    ) -> dict[str, ColumnBreak]:
        """Колонки объекта, которые мешают переводу, по имени."""
        broken: dict[str, ColumnBreak] = {}
        for part in change.parts:
            if part.part is not PartKind.COLUMN:
                continue

            item = cls._column_break(part, widens)
            if item is not None:
                broken[item.column] = item

        return broken

    @staticmethod
    def _column_break(part: PartChange, widens: TypeWidens) -> ColumnBreak | None:
        if part.status is ChangeStatus.REMOVED:
            return ColumnBreak(column=part.name, reason=StaleReason.COLUMN_REMOVED)

        if part.status is not ChangeStatus.MODIFIED:
            return None

        for field in part.fields:
            if field.field != StaleDetail.TYPE.value:
                continue

            was = ""
            if field.was is not None:
                was = field.was

            now = ""
            if field.now is not None:
                now = field.now

            if widens(was, now):
                return None

            return ColumnBreak(
                column=part.name,
                reason=StaleReason.COLUMN_CHANGED,
                type_change=f"{was} -> {now}",
            )

        return None

    @staticmethod
    def _node_stale(
        node: Node, broken: Mapping[str, ColumnBreak], gap: ConnectionGap
    ) -> Iterator[Stale]:
        target = EntityRef(kind=EntityKind.NODE, id=node.id)
        for item in sorted(broken.values(), key=attrgetter("column")):
            yield gap.stale(target, item.reason, item.detail())

    @classmethod
    def _flow_stale(
        cls,
        process: CatalogSnapshot,
        flow: Flow,
        changes: Mapping[ChangeKey, ObjectChange],
        breaks: Mapping[UUID, Mapping[str, ColumnBreak]],
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

            if change.status is ChangeStatus.REMOVED:
                for column in flow.columns_at(end):
                    detail = {
                        StaleDetail.SIDE.value: end.value,
                        StaleDetail.COLUMN.value: column,
                    }
                    yield gap.stale(target, StaleReason.COLUMN_REMOVED, detail)
                continue

            broken = breaks.get(node.id, {})
            for column in flow.columns_at(end):
                item = broken.get(column)
                if item is None:
                    continue

                yield gap.stale(target, item.reason, item.detail(end))
