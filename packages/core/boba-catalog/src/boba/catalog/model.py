"""Домен процесса загрузки: слои, узлы-ссылки на объекты источников, виды
загрузки с полями, потоки со значениями; снимок версии и его инварианты.

Снимок это полное состояние процесса одной версии: словари слоёв, узлов,
видов загрузки и потоков по id. Он неизменяем: операции из boba.catalog.ops
получают новый снимок методами added/replaced/removed и после каждой
операции зовут check(). Колонки у узла не хранятся: они читаются из версии
источника по адресу, поэтому ссылки на колонки в значениях потоков — по
имени, а их наличие проверяется отдельно, по снимкам источников
(check_against).

Ошибки:
CatalogInvariantError — снимок или значения потока нарушают инварианты,
    перечень нарушений в violations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import Field, model_validator

from boba.catalog.base import CatalogError, CatalogInvariantError, CatalogModel
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = [
    "CatalogEntity",
    "CatalogError",
    "CatalogInvariantError",
    "CatalogModel",
    "CatalogSnapshot",
    "ColumnSide",
    "EntityKind",
    "EntityRef",
    "Flow",
    "Layer",
    "LoadField",
    "LoadFieldType",
    "LoadKind",
    "LoadSpec",
    "LoadValue",
    "Node",
    "ObjectResolver",
]

LoadValue = str | int | bool | tuple[str, ...] | ObjectRef

KeyT = TypeVar("KeyT", bound=Hashable)


class ColumnSide(StrEnum):
    """С какого конца потока берутся колонки поля вида."""

    SOURCE = "source"
    TARGET = "target"
    ANY = "any"


class LoadFieldType(StrEnum):
    """Тип поля вида загрузки; знает форму хранения значения."""

    TEXT = "text"
    INT = "int"
    BOOL = "bool"
    COLUMN = "column"
    COLUMNS = "columns"
    ROUTINE = "routine"

    def accepts(self, value: LoadValue) -> bool:
        """Значение имеет форму хранения этого типа."""
        if self is LoadFieldType.TEXT:
            return isinstance(value, str)

        if self is LoadFieldType.INT:
            return self._is_int(value)

        if self is LoadFieldType.BOOL:
            return isinstance(value, bool)

        if self is LoadFieldType.COLUMN:
            return isinstance(value, str) and value != ""

        if self is LoadFieldType.COLUMNS:
            return self._is_columns(value)

        return isinstance(value, ObjectRef) and value.kind is ObjectKind.ROUTINE

    @staticmethod
    def _is_int(value: LoadValue) -> bool:
        if isinstance(value, bool):
            return False

        return isinstance(value, int)

    @staticmethod
    def _is_columns(value: LoadValue) -> bool:
        if not isinstance(value, tuple):
            return False

        return len(value) > 0

    def column_names(self, value: LoadValue) -> tuple[str, ...]:
        """Имена колонок внутри значения; у остальных типов пусто."""
        if self is LoadFieldType.COLUMN:
            if isinstance(value, str):
                return (value,)

            return ()

        if self is LoadFieldType.COLUMNS:
            if isinstance(value, tuple):
                return value

            return ()

        return ()

    @staticmethod
    def shape_of(value: LoadValue) -> str:
        """Форма значения для текста нарушения."""
        if isinstance(value, tuple):
            return f"list of {len(value)}"

        if isinstance(value, ObjectRef):
            return f"{value.kind.value} ref"

        return type(value).__name__


class LoadField(CatalogModel):
    """Поле вида загрузки: имя, тип, сторона потока для колонок, обязательность."""

    name: str = Field(min_length=1)
    type: LoadFieldType
    side: ColumnSide = ColumnSide.ANY
    required: bool
    description: str = ""


class LoadSpec(CatalogModel):
    """Правило загрузки потока: вид и значения по его полям."""

    kind_id: UUID
    values: Mapping[str, LoadValue]


class LoadKind(CatalogModel):
    """Вид загрузки, заведённый пользователем: имя и описание полей.

    Поток хранит значения по именам полей вида; вид проверяет их состав и
    типы (violations_of) и отдаёт ссылки на колонки по сторонам (column_refs).
    """

    id: UUID
    name: str = Field(min_length=1)
    description: str = ""
    fields: tuple[LoadField, ...]

    @model_validator(mode="after")
    def _unique_field_names(self) -> LoadKind:
        names: list[str] = []
        for field in self.fields:
            names.append(field.name)

        repeated = sorted(set(CatalogSnapshot.repeated(names)))
        if repeated:
            msg = f"load kind {self.name!r}: duplicate field names {repeated}"
            raise ValueError(msg)

        return self

    def field_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for field in self.fields:
            names.append(field.name)

        return tuple(names)

    def violations_of(self, spec: LoadSpec) -> Iterator[str]:
        """Нарушения состава и типов значений относительно полей вида."""
        by_name = self._fields_by_name()

        for name in spec.values:
            if name in by_name:
                continue

            yield f"unknown field {name!r} of load kind {self.name!r}"

        for field in self.fields:
            if field.name not in spec.values:
                if field.required:
                    yield (
                        f"required field {field.name!r} of load kind {self.name!r} "
                        f"is missing"
                    )

                continue

            value = spec.values[field.name]
            if field.type.accepts(value):
                continue

            shape = LoadFieldType.shape_of(value)
            yield (
                f"field {field.name!r} of load kind {self.name!r} "
                f"expects {field.type.value}, got {shape}"
            )

    def column_refs(self, spec: LoadSpec) -> Iterator[tuple[LoadField, str]]:
        """Пары (поле, имя колонки) по всем ссылкам на колонки в значениях."""
        by_name = self._fields_by_name()

        for name, value in spec.values.items():
            field = by_name.get(name)
            if field is None:
                continue

            for column in field.type.column_names(value):
                yield field, column

    def routine_refs(self, spec: LoadSpec) -> Iterator[tuple[LoadField, ObjectRef]]:
        by_name = self._fields_by_name()

        for name, value in spec.values.items():
            field = by_name.get(name)
            if field is None:
                continue

            if field.type is not LoadFieldType.ROUTINE:
                continue

            if not isinstance(value, ObjectRef):
                continue

            yield field, value

    def _fields_by_name(self) -> dict[str, LoadField]:
        by_name: dict[str, LoadField] = {}
        for field in self.fields:
            by_name[field.name] = field

        return by_name


class Layer(CatalogModel):
    """Слой хранения: дорожка на диаграмме, порядок слева направо по position."""

    id: UUID
    name: str = Field(min_length=1)
    position: int = Field(ge=0)
    description: str = ""


class Node(CatalogModel):
    """Объект источника, поставленный в слой; колонки читаются из источника."""

    id: UUID
    layer_id: UUID
    ref: ObjectRef
    alias: str | None = None
    note: str = ""

    @property
    def label(self) -> str:
        if self.alias is not None and self.alias != "":
            return self.alias

        return self.ref.path[-1]


class Flow(CatalogModel):
    """Поток из узла в узел с правилом загрузки."""

    id: UUID
    from_node_id: UUID
    to_node_id: UUID
    load: LoadSpec
    description: str = ""


CatalogEntity = Layer | Node | LoadKind | Flow


class EntityKind(StrEnum):
    """Виды сущностей снимка; значение — имя таблицы хранения."""

    LAYER = "layer"
    NODE = "node"
    LOAD_KIND = "load_kind"
    FLOW = "flow"

    @classmethod
    def of(cls, entity: CatalogEntity) -> EntityKind:
        if isinstance(entity, Layer):
            return cls.LAYER

        if isinstance(entity, Node):
            return cls.NODE

        if isinstance(entity, LoadKind):
            return cls.LOAD_KIND

        return cls.FLOW

    @property
    def table_field(self) -> str:
        """Имя поля снимка с таблицей сущностей этого вида."""
        if self is EntityKind.LAYER:
            return "layers"

        if self is EntityKind.NODE:
            return "nodes"

        if self is EntityKind.LOAD_KIND:
            return "load_kinds"

        return "flows"


class EntityRef(CatalogModel):
    """Ссылка на сущность снимка: вид и id."""

    kind: EntityKind
    id: UUID

    @classmethod
    def of(cls, entity: CatalogEntity) -> EntityRef:
        return cls(kind=EntityKind.of(entity), id=entity.id)


class ObjectResolver(Protocol):
    """Что домен знает об объектах источников при проверке значений потоков:
    существует ли объект и какие у него колонки. Реализует сервис по
    привязанным версиям источников."""

    def exists(self, ref: ObjectRef) -> bool: ...

    def columns_of(self, ref: ObjectRef) -> Sequence[str] | None: ...


class CatalogSnapshot(CatalogModel):
    """Полное состояние процесса одной версии.

    Таблицы сущностей по id. Методы added/replaced/removed возвращают новый
    снимок, не меняя текущий; check() проверяет внутренние инварианты,
    check_against() — ссылки на объекты и колонки источников.
    """

    layers: Mapping[UUID, Layer]
    nodes: Mapping[UUID, Node]
    load_kinds: Mapping[UUID, LoadKind]
    flows: Mapping[UUID, Flow]

    @classmethod
    def empty(cls) -> CatalogSnapshot:
        return cls(layers={}, nodes={}, load_kinds={}, flows={})

    def table(self, kind: EntityKind) -> Mapping[UUID, CatalogEntity]:
        if kind is EntityKind.LAYER:
            return self.layers

        if kind is EntityKind.NODE:
            return self.nodes

        if kind is EntityKind.LOAD_KIND:
            return self.load_kinds

        return self.flows

    def added(self, entity: CatalogEntity) -> CatalogSnapshot:
        """Снимок с новой сущностью.

        Ошибки:
        CatalogInvariantError — id уже занят.
        """
        ref = EntityRef.of(entity)
        table = dict(self.table(ref.kind))
        if entity.id in table:
            msg = f"{self.label(ref)} already exists in the catalog (id {entity.id})"
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
            msg = f"{self.label(ref)} not found in the catalog"
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
            msg = f"{self.label(ref)} not found in the catalog"
            raise CatalogInvariantError([msg])

        del table[ref.id]
        return self._with_table(ref.kind, table)

    def restricted(
        self, node_ids: Iterable[UUID], layer_ids: Iterable[UUID]
    ) -> CatalogSnapshot:
        """Срез по фильтру диаграммы: узлы из списка и из перечисленных слоёв,
        их слои, потоки между ними и виды загрузки этих потоков. Пустой фильтр —
        весь процесс."""
        chosen_nodes = frozenset(node_ids)
        chosen_layers = frozenset(layer_ids)
        if not chosen_nodes and not chosen_layers:
            return self

        nodes = dict(self._chosen_nodes(chosen_nodes, chosen_layers))
        layers = dict(self._layers_of(nodes.values(), chosen_layers))
        flows = dict(self._flows_between(nodes))
        load_kinds = dict(self._kinds_of(flows.values()))

        return CatalogSnapshot(
            layers=layers, nodes=nodes, load_kinds=load_kinds, flows=flows
        )

    def _chosen_nodes(
        self, node_ids: frozenset[UUID], layer_ids: frozenset[UUID]
    ) -> Iterator[tuple[UUID, Node]]:
        for node in self.nodes.values():
            if node.id in node_ids:
                yield node.id, node
                continue

            if node.layer_id in layer_ids:
                yield node.id, node

    def _layers_of(
        self, nodes: Iterable[Node], layer_ids: frozenset[UUID]
    ) -> Iterator[tuple[UUID, Layer]]:
        wanted = set(layer_ids)
        for node in nodes:
            wanted.add(node.layer_id)

        for layer in self.layers.values():
            if layer.id not in wanted:
                continue

            yield layer.id, layer

    def _flows_between(self, nodes: Mapping[UUID, Node]) -> Iterator[tuple[UUID, Flow]]:
        for flow in self.flows.values():
            if flow.from_node_id not in nodes:
                continue

            if flow.to_node_id not in nodes:
                continue

            yield flow.id, flow

    def _kinds_of(self, flows: Iterable[Flow]) -> Iterator[tuple[UUID, LoadKind]]:
        used: set[UUID] = set()
        for flow in flows:
            used.add(flow.load.kind_id)

        for kind in self.load_kinds.values():
            if kind.id not in used:
                continue

            yield kind.id, kind

    def nodes_in(self, layer_id: UUID) -> Iterator[Node]:
        for node in self.nodes.values():
            if node.layer_id != layer_id:
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

    def flows_of_kind(self, kind_id: UUID) -> Iterator[Flow]:
        for flow in self.flows.values():
            if flow.load.kind_id != kind_id:
                continue

            yield flow

    def sources(self) -> set[UUID]:
        """Источники, на объекты которых ссылаются узлы и рутины потоков."""
        used: set[UUID] = set()
        for node in self.nodes.values():
            used.add(node.ref.source_id)

        for flow in self.flows.values():
            kind = self.load_kinds.get(flow.load.kind_id)
            if kind is None:
                continue

            for _field, ref in kind.routine_refs(flow.load):
                used.add(ref.source_id)

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
        """Ссылки на объекты и колонки источников по привязанным версиям.

        Ошибки:
        CatalogInvariantError — объекта нет, колонки нет у объекта нужной
            стороны, рутина не найдена.
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
        kind = self.load_kinds.get(flow.load.kind_id)
        if kind is None:
            return

        label = self._flow_label(flow)
        for field, column in kind.column_refs(flow.load):
            allowed = self._side_columns(flow, field.side, resolver)
            if allowed is None:
                continue

            if column in allowed:
                continue

            yield (
                f"{label}: field {field.name!r} names column {column!r}"
                f" that is not on the {field.side.value} side"
            )

        for field, ref in kind.routine_refs(flow.load):
            if resolver.exists(ref):
                continue

            yield (
                f"{label}: field {field.name!r} names a missing routine"
                f" {ref.render()!r}"
            )

    def _side_columns(
        self, flow: Flow, side: ColumnSide, resolver: ObjectResolver
    ) -> set[str] | None:
        """Колонки концов потока по стороне поля; None — ни об одном конце
        источник ничего не знает, проверять нечего."""
        ends: list[UUID] = []
        if side is not ColumnSide.TARGET:
            ends.append(flow.from_node_id)

        if side is not ColumnSide.SOURCE:
            ends.append(flow.to_node_id)

        known: set[str] | None = None
        for node_id in ends:
            node = self.nodes.get(node_id)
            if node is None:
                continue

            columns = resolver.columns_of(node.ref)
            if columns is None:
                continue

            if known is None:
                known = set()

            known.update(columns)

        return known

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
        yield from self._load_values()

    def _layer_names(self) -> Iterator[str]:
        for layer in self.layers.values():
            yield layer.name

    def _layer_positions(self) -> Iterator[int]:
        for layer in self.layers.values():
            yield layer.position

    def _load_kind_names(self) -> Iterator[str]:
        for kind in self.load_kinds.values():
            yield kind.name

    def _duplicate_names(self) -> Iterator[str]:
        for name in self.repeated(self._layer_names()):
            yield f"duplicate layer name {name!r}"

        for position in self.repeated(self._layer_positions()):
            yield f"duplicate layer position {position}"

        for name in self.repeated(self._load_kind_names()):
            yield f"duplicate load kind name {name!r}"

    def _node_refs(self) -> Iterator[tuple[UUID, ObjectKind, tuple[str, ...]]]:
        for node in self.nodes.values():
            yield node.ref.source_id, node.ref.kind, node.ref.path

    def _duplicate_refs(self) -> Iterator[str]:
        for _source, kind, path in self.repeated(self._node_refs()):
            yield f"object {kind.value} {'/'.join(path)!r} is placed twice"

    def _dangling_references(self) -> Iterator[str]:
        for node in self.nodes.values():
            if node.layer_id in self.layers:
                continue

            label = self.label(EntityRef.of(node))
            yield f"{label} refers to a missing layer {node.layer_id}"

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

        if flow.load.kind_id not in self.load_kinds:
            yield f"{label}: load kind {flow.load.kind_id} is missing"

    def _load_values(self) -> Iterator[str]:
        for flow in self.flows.values():
            kind = self.load_kinds.get(flow.load.kind_id)
            if kind is None:
                continue

            label = self._flow_label(flow)
            for violation in kind.violations_of(flow.load):
                yield f"{label}: {violation}"
