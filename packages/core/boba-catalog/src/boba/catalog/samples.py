"""Образец процесса для тестов и стендов: узлы над объектами подключения
prod из образца Postgres (boba.db.postgres.snapshot_sample) с позициями на
холсте, две группы и потоки с парами колонок. Сам снимок подключения здесь
не нужен: узлы ссылаются на объекты адресами."""

from __future__ import annotations

from uuid import UUID

from boba.catalog.model import CatalogSnapshot, ColumnLink, Flow, Group, Node, Position
from boba.catalog.ops import AddFlow, AddGroup, AddNode, OperationList
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = ["ProcessSample", "SampleIds"]


class SampleIds:
    """Идентификаторы подключений образцов."""

    POSTGRES = UUID(int=0x5001)
    CLICKHOUSE = UUID(int=0x5002)


class ProcessSample:
    """Процесс над образцом Postgres: группы raw и dm, узлы orders и customers
    в raw, v_orders в dm, процедура load_orders без группы и позиции; поток
    orders → v_orders переносит колонки id и amount, поток customers →
    v_orders — без пар."""

    def __init__(self, connection_id: UUID = SampleIds.POSTGRES) -> None:
        self.connection_id = connection_id
        self.raw = Group(id=UUID(int=0x7101), name="raw")
        self.dm = Group(id=UUID(int=0x7102), name="dm")

        self.orders = Node(
            id=UUID(int=0x7201),
            ref=self.ref(ObjectKind.RELATION, ("prod", "public", "orders")),
            position=Position(x=0, y=0),
            group_id=self.raw.id,
        )
        self.customers = Node(
            id=UUID(int=0x7202),
            ref=self.ref(ObjectKind.RELATION, ("prod", "public", "customers")),
            position=Position(x=0, y=240),
            group_id=self.raw.id,
            alias="clients",
        )
        self.v_orders = Node(
            id=UUID(int=0x7203),
            ref=self.ref(ObjectKind.RELATION, ("prod", "public", "v_orders")),
            position=Position(x=420, y=120),
            group_id=self.dm.id,
        )
        self.load_orders = Node(
            id=UUID(int=0x7204),
            ref=self.ref(ObjectKind.ROUTINE, ("prod", "etl", "load_orders", "date")),
        )

        self.flow_orders = Flow(
            id=UUID(int=0x7401),
            from_node_id=self.orders.id,
            to_node_id=self.v_orders.id,
            columns=(
                ColumnLink(from_column="id", to_column="id"),
                ColumnLink(from_column="amount", to_column="id"),
            ),
            description="orders feed the view",
        )
        self.flow_customers = Flow(
            id=UUID(int=0x7402),
            from_node_id=self.customers.id,
            to_node_id=self.v_orders.id,
        )

    def ref(self, kind: ObjectKind, path: tuple[str, ...]) -> ObjectRef:
        return ObjectRef(connection_id=self.connection_id, kind=kind, path=path)

    def snapshot(self) -> CatalogSnapshot:
        return CatalogSnapshot(
            groups={self.raw.id: self.raw, self.dm.id: self.dm},
            nodes={
                self.orders.id: self.orders,
                self.customers.id: self.customers,
                self.v_orders.id: self.v_orders,
                self.load_orders.id: self.load_orders,
            },
            flows={
                self.flow_orders.id: self.flow_orders,
                self.flow_customers.id: self.flow_customers,
            },
        )

    def ops(self) -> OperationList:
        """Тот же процесс как список операций от пустого снимка."""
        return OperationList(
            root=(
                AddGroup(group=self.raw),
                AddGroup(group=self.dm),
                AddNode(node=self.orders),
                AddNode(node=self.customers),
                AddNode(node=self.v_orders),
                AddNode(node=self.load_orders),
                AddFlow(flow=self.flow_orders),
                AddFlow(flow=self.flow_customers),
            )
        )
