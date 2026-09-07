"""Домен процесса: узлы-ссылки на объекты подключений с позицией на холсте,
необязательные группы узлов, потоки с парами колонок; снимок версии процесса
и его инварианты.

Снимок это полное состояние процесса одной версии: словари групп, узлов и
потоков по id. Он неизменяем: операции из boba.catalog.ops получают новый
снимок методами added/replaced/removed и после каждой операции зовут
check(). Колонки у узла не хранятся: они читаются из версии снимка
подключения по адресу, поэтому пары колонок потока — по именам, а их наличие
проверяется отдельно, по снимкам подключений (check_against).

Ошибки:
CatalogInvariantError — снимок или пары колонок потока нарушают инварианты,
    перечень нарушений в violations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import Field

from boba.catalog.base import CatalogError, CatalogInvariantError, CatalogModel
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = [
    "CatalogEntity",
    "CatalogError",
    "CatalogInvariantError",
    "CatalogModel",
    "CatalogSnapshot",
    "ColumnLink",
    "EntityKind",
    "EntityRef",
    "Flow",
    "FlowEnd",
    "Group",
    "Node",
    "ObjectResolver",
    "Position",
]

KeyT = TypeVar("KeyT", bound=Hashable)


class FlowEnd(StrEnum):
    """Конец потока: откуда данные уходят и куда приходят."""

    SOURCE = "source"
    TARGET = "target"


class ColumnLink(CatalogModel):
    """Переход колонки: из колонки узла-источника в колонку узла-приёмника."""

    from_column: str = Field(min_length=1)
    to_column: str = Field(min_length=1)

    def render(self) -> str:
        return f"{self.from_column} -> {self.to_column}"

    def column_at(self, end: FlowEnd) -> str:
        if end is FlowEnd.SOURCE:
            return self.from_column

        return self.to_column


class Group(CatalogModel):
    """Именованная область холста: рамка вокруг узлов, которые в ней состоят.
    Своих размеров нет, рамка считается по узлам."""

    id: UUID
    name: str = Field(min_length=1)


class Position(CatalogModel):
    """Место карточки узла на холсте."""

    x: float
    y: float


class Node(CatalogModel):
    """Объект подключения на холсте; колонки читаются из снимка. Без позиции
    узел раскладывает автолэйаут, группа не обязательна."""

    id: UUID
    ref: ObjectRef
    position: Position | None = None
    group_id: UUID | None = None
    alias: str | None = None
    note: str = ""

    @property
    def label(self) -> str:
        if self.alias is not None and self.alias != "":
            return self.alias

        return self.ref.path[-1]


class Flow(CatalogModel):
    """Поток из узла в узел: какие колонки источника переходят в какие
    колонки приёмника, и описание словами."""

    id: UUID
    from_node_id: UUID
    to_node_id: UUID
    columns: tuple[ColumnLink, ...] = ()
    description: str = ""

    def node_at(self, end: FlowEnd) -> UUID:
        if end is FlowEnd.SOURCE:
            return self.from_node_id

        return self.to_node_id

    def columns_at(self, end: FlowEnd) -> Iterator[str]:
        """Имена колонок, которые поток именует на этом конце."""
        for link in self.columns:
            yield link.column_at(end)

    def duplicate_links(self) -> Iterator[ColumnLink]:
        seen: set[tuple[str, str]] = set()
        for link in self.columns:
            key = (link.from_column, link.to_column)
            if key in seen:
                yield link
                continue

            seen.add(key)


CatalogEntity = Group | Node | Flow


class EntityKind(StrEnum):
    """Виды сущностей снимка; значение — имя таблицы хранения."""

    GROUP = "group"
    NODE = "node"
    FLOW = "flow"

    @classmethod
    def of(cls, entity: CatalogEntity) -> EntityKind:
        if isinstance(entity, Group):
            return cls.GROUP

        if isinstance(entity, Node):
            return cls.NODE

        return cls.FLOW

    @property
    def table_field(self) -> str:
        """Имя поля снимка с таблицей сущностей этого вида."""
        if self is EntityKind.GROUP:
            return "groups"

        if self is EntityKind.NODE:
            return "nodes"

        return "flows"


class EntityRef(CatalogModel):
    """Ссылка на сущность снимка: вид и id."""

    kind: EntityKind
    id: UUID

    @classmethod
    def of(cls, entity: CatalogEntity) -> EntityRef:
        return cls(kind=EntityKind.of(entity), id=entity.id)


class ObjectResolver(Protocol):
    """Что домен знает об объектах подключений при проверке потоков:
    существует ли объект и какие у него колонки. Реализует сервис по
    привязанным версиям снимков."""

    def exists(self, ref: ObjectRef) -> bool: ...

    def columns_of(self, ref: ObjectRef) -> Sequence[str] | None: ...


class CatalogSnapshot(CatalogModel):
    """Полное состояние процесса одной версии.

    Таблицы сущностей по id. Методы added/replaced/removed возвращают новый
    снимок, не меняя текущий; check() проверяет внутренние инварианты,
    check_against() — ссылки на объекты и колонки подключений.
    """

    groups: Mapping[UUID, Group]
    nodes: Mapping[UUID, Node]
    flows: Mapping[UUID, Flow]

    @classmethod
    def empty(cls) -> CatalogSnapshot:
        return cls(groups={}, nodes={}, flows={})

    def table(self, kind: EntityKind) -> Mapping[UUID, CatalogEntity]:
        if kind is EntityKind.GROUP:
            return self.groups

        if kind is EntityKind.NODE:
            return self.nodes

        return self.flows

    def added(self, entity: CatalogEntity) -> CatalogSnapshot:
        """Снимок с новой сущностью.

        Ошибки:
        CatalogInvariantError — id уже занят.
        """
        ref = EntityRef.of(entity)
        table = dict(self.table(ref.kind))
        if entity.id in table:
            msg = f"{self.label(ref)} already exists in the process (id {entity.id})"
            raise CatalogInvariantError([msg])

        table[entity.id] = entity
        return self._with_table(ref.kind, table)

    def replaced(self, entity: CatalogEntity) -> CatalogSnapshot:
        """Снимок, где сущность с этим id заменена целиком.

        Ошибки:
        CatalogInvariantError — сущности с таким id нет.
        """
        ref = EntityRef.of(entity)
        table = dict(self.table(ref.kind))
        if entity.id not in table:
            msg = f"{self.label(ref)} not found in the process"
            raise CatalogInvariantError([msg])

        table[entity.id] = entity
        return self._with_table(ref.kind, table)

    def removed(self, ref: EntityRef) -> CatalogSnapshot:
        """Снимок без сущности; зависимые не трогаются, их проверяет операция.

        Ошибки:
        CatalogInvariantError — сущности с таким id нет.
        """
        table = dict(self.table(ref.kind))
        if ref.id not in table:
            msg = f"{self.label(ref)} not found in the process"
            raise CatalogInvariantError([msg])

        del table[ref.id]
        return self._with_table(ref.kind, table)

    def nodes_in(self, group_id: UUID) -> Iterator[Node]:
        for node in self.nodes.values():
            if node.group_id != group_id:
                continue

            yield node

    def node_of(self, ref: ObjectRef) -> Node | None:
        for node in self.nodes.values():
            if node.ref == ref:
                return node

        return None

    def flows_of(self, node_id: UUID) -> Iterator[Flow]:
        for flow in self.flows.values():
            if flow.from_node_id == node_id:
                yield flow
                continue

            if flow.to_node_id == node_id:
                yield flow

    def connections(self) -> set[UUID]:
        """Подключения, на объекты которых ссылаются узлы."""
        used: set[UUID] = set()
        for node in self.nodes.values():
            used.add(node.ref.connection_id)

        return used

    def label(self, ref: EntityRef) -> str:
        """Подпись сущности для сообщений: вид и имя, без имени — id."""
        entity = self.table(ref.kind).get(ref.id)
        if entity is None:
            return f"{ref.kind.value} {ref.id}"

        if isinstance(entity, Flow):
            return self._flow_label(entity)

        if isinstance(entity, Node):
            return f"node {entity.ref.render()!r}"

        return f"{ref.kind.value} {entity.name!r}"

    def check(self) -> None:
        """Внутренние инварианты снимка целиком.

        Ошибки:
        CatalogInvariantError — с перечнем нарушений.
        """
        violations = list(self._violations())
        if violations:
            raise CatalogInvariantError(violations)

    def check_against(self, resolver: ObjectResolver) -> None:
        """Ссылки на объекты и колонки подключений по привязанным версиям.

        Ошибки:
        CatalogInvariantError — объекта нет, колонки нет у узла нужного конца.
        """
        violations = list(self.source_violations(resolver))
        if violations:
            raise CatalogInvariantError(violations)

    def source_violations(self, resolver: ObjectResolver) -> Iterator[str]:
        for node in self.nodes.values():
            if resolver.exists(node.ref):
                continue

            yield f"node {node.ref.render()!r} points to a missing object"

        for flow in self.flows.values():
            yield from self._flow_source_violations(flow, resolver)

    def _flow_source_violations(
        self, flow: Flow, resolver: ObjectResolver
    ) -> Iterator[str]:
        label = self._flow_label(flow)
        for end in FlowEnd:
            known = self._end_columns(flow, end, resolver)
            if known is None:
                continue

            for column in flow.columns_at(end):
                if column in known:
                    continue

                yield (f"{label}: column {column!r} is not on the {end.value} side")

    def _end_columns(
        self, flow: Flow, end: FlowEnd, resolver: ObjectResolver
    ) -> set[str] | None:
        """Колонки узла на конце потока; None — снимок про узел ничего не
        знает, проверять нечего."""
        node = self.nodes.get(flow.node_at(end))
        if node is None:
            return None

        columns = resolver.columns_of(node.ref)
        if columns is None:
            return None

        return set(columns)

    @staticmethod
    def repeated(keys: Iterable[KeyT]) -> Iterator[KeyT]:
        """Ключи, встречающиеся больше одного раза."""
        counts = Counter(keys)
        for key, count in counts.items():
            if count == 1:
                continue

            yield key

    def _with_table(
        self, kind: EntityKind, table: Mapping[UUID, CatalogEntity]
    ) -> CatalogSnapshot:
        return self.model_copy(update={kind.table_field: dict(table)})

    def _node_label(self, node_id: UUID) -> str:
        return self.label(EntityRef(kind=EntityKind.NODE, id=node_id))

    def _flow_label(self, flow: Flow) -> str:
        source = self._node_label(flow.from_node_id)
        target = self._node_label(flow.to_node_id)
        return f"flow {source} -> {target}"

    def _violations(self) -> Iterator[str]:
        yield from self._duplicate_names()
        yield from self._duplicate_refs()
        yield from self._dangling_references()
        yield from self._duplicate_links()

    def _group_names(self) -> Iterator[str]:
        for group in self.groups.values():
            yield group.name

    def _duplicate_names(self) -> Iterator[str]:
        for name in self.repeated(self._group_names()):
            yield f"duplicate group name {name!r}"

    def _node_refs(self) -> Iterator[tuple[UUID, ObjectKind, tuple[str, ...]]]:
        for node in self.nodes.values():
            yield node.ref.connection_id, node.ref.kind, node.ref.path

    def _duplicate_refs(self) -> Iterator[str]:
        for _connection, kind, path in self.repeated(self._node_refs()):
            yield f"object {kind.value} {'/'.join(path)!r} is placed twice"

    def _dangling_references(self) -> Iterator[str]:
        for node in self.nodes.values():
            if node.group_id is None:
                continue

            if node.group_id in self.groups:
                continue

            label = self.label(EntityRef.of(node))
            yield f"{label} refers to a missing group {node.group_id}"

        for flow in self.flows.values():
            yield from self._flow_references(flow)

    def _flow_references(self, flow: Flow) -> Iterator[str]:
        label = self._flow_label(flow)
        if flow.from_node_id not in self.nodes:
            yield f"{label}: source node is missing"

        if flow.to_node_id not in self.nodes:
            yield f"{label}: target node is missing"

        if flow.from_node_id == flow.to_node_id:
            yield f"{label}: a flow cannot loop on one node"

    def _duplicate_links(self) -> Iterator[str]:
        for flow in self.flows.values():
            label = self._flow_label(flow)
            for link in flow.duplicate_links():
                yield f"{label}: column pair {link.render()!r} is listed twice"
