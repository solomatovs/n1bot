"""Образец процесса для тестов и стендов: два слоя, узлы над объектами
источника prod из образца Postgres (boba.db.postgres.snapshot_sample), виды
загрузки и потоки. Сам снимок источника здесь не нужен: узлы ссылаются на
объекты адресами."""

from __future__ import annotations

from uuid import UUID

from boba.catalog.model import (
    CatalogSnapshot,
    ColumnSide,
    Flow,
    Layer,
    LoadField,
    LoadFieldType,
    LoadKind,
    LoadSpec,
    Node,
)
from boba.catalog.ops import AddFlow, AddLayer, AddLoadKind, AddNode, OperationList
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = ["ProcessSample", "SampleIds"]


class SampleIds:
    """Идентификаторы источников образцов."""

    POSTGRES = UUID(int=0x5001)
    CLICKHOUSE = UUID(int=0x5002)


class ProcessSample:
    """Процесс над образцом Postgres: слои raw и dm, узлы orders, customers,
    v_orders и процедура load_orders, виды full и hashkey, поток orders →
    v_orders по hashkey с колонкой id и реализацией процедурой."""

    def __init__(self, source_id: UUID = SampleIds.POSTGRES) -> None:
        self.source_id = source_id
        self.raw = Layer(id=UUID(int=0x7101), name="raw", position=0)
        self.dm = Layer(id=UUID(int=0x7102), name="dm", position=1)

        self.orders = Node(
            id=UUID(int=0x7201),
            layer_id=self.raw.id,
            ref=self.ref(ObjectKind.RELATION, ("prod", "public", "orders")),
        )
        self.customers = Node(
            id=UUID(int=0x7202),
            layer_id=self.raw.id,
            ref=self.ref(ObjectKind.RELATION, ("prod", "public", "customers")),
            alias="clients",
        )
        self.v_orders = Node(
            id=UUID(int=0x7203),
            layer_id=self.dm.id,
            ref=self.ref(ObjectKind.RELATION, ("prod", "public", "v_orders")),
        )
        self.load_orders = Node(
            id=UUID(int=0x7204),
            layer_id=self.dm.id,
            ref=self.ref(ObjectKind.ROUTINE, ("prod", "etl", "load_orders", "date")),
        )

        self.full = LoadKind(id=UUID(int=0x7301), name="full", fields=())
        self.hashkey = LoadKind(
            id=UUID(int=0x7302),
            name="hashkey",
            fields=(
                LoadField(
                    name="hash_columns",
                    type=LoadFieldType.COLUMNS,
                    side=ColumnSide.SOURCE,
                    required=True,
                ),
                LoadField(
                    name="implemented_by", type=LoadFieldType.ROUTINE, required=False
                ),
                LoadField(name="batch", type=LoadFieldType.INT, required=False),
            ),
        )

        self.flow_orders = Flow(
            id=UUID(int=0x7401),
            from_node_id=self.orders.id,
            to_node_id=self.v_orders.id,
            load=LoadSpec(
                kind_id=self.hashkey.id,
                values={
                    "hash_columns": ("id", "amount"),
                    "implemented_by": self.load_orders.ref,
                    "batch": 1000,
                },
            ),
        )
        self.flow_customers = Flow(
            id=UUID(int=0x7402),
            from_node_id=self.customers.id,
            to_node_id=self.v_orders.id,
            load=LoadSpec(kind_id=self.full.id, values={}),
        )

    def ref(self, kind: ObjectKind, path: tuple[str, ...]) -> ObjectRef:
        return ObjectRef(source_id=self.source_id, kind=kind, path=path)

    def snapshot(self) -> CatalogSnapshot:
        return CatalogSnapshot(
            layers={self.raw.id: self.raw, self.dm.id: self.dm},
            nodes={
                self.orders.id: self.orders,
                self.customers.id: self.customers,
                self.v_orders.id: self.v_orders,
                self.load_orders.id: self.load_orders,
            },
            load_kinds={self.full.id: self.full, self.hashkey.id: self.hashkey},
            flows={
                self.flow_orders.id: self.flow_orders,
                self.flow_customers.id: self.flow_customers,
            },
        )

    def ops(self) -> OperationList:
        """Тот же процесс как список операций от пустого снимка."""
        return OperationList(
            root=(
                AddLayer(layer=self.raw),
                AddLayer(layer=self.dm),
                AddNode(node=self.orders),
                AddNode(node=self.customers),
                AddNode(node=self.v_orders),
                AddNode(node=self.load_orders),
                AddLoadKind(load_kind=self.full),
                AddLoadKind(load_kind=self.hashkey),
                AddFlow(flow=self.flow_orders),
                AddFlow(flow=self.flow_customers),
            )
        )
