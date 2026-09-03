"""Образец каталога: три слоя, пять наборов, два вида загрузки, три потока."""

from __future__ import annotations

from uuid import UUID

from boba.catalog import (
    AddColumn,
    AddDataset,
    AddFlow,
    AddLayer,
    AddLoadKind,
    CatalogOp,
    CatalogSnapshot,
    Column,
    Dataset,
    Flow,
    Layer,
    LoadField,
    LoadFieldType,
    LoadKind,
    LoadSpec,
    OperationList,
)


class Sample:
    """Реалистичный каталог raw -> stg -> dm с детерминированными id."""

    def __init__(self) -> None:
        self._next_column_id = 19

        self.raw = Layer(id=UUID(int=1), name="raw")
        self.stg = Layer(id=UUID(int=2), name="stg")
        self.dm = Layer(id=UUID(int=3), name="dm")

        self.raw_orders = Dataset(
            id=UUID(int=10),
            layer_id=self.raw.id,
            name="orders",
            source="erp",
            owner="ingest",
        )
        self.raw_customers = Dataset(
            id=UUID(int=11), layer_id=self.raw.id, name="customers", source="crm"
        )
        self.stg_orders = Dataset(
            id=UUID(int=12), layer_id=self.stg.id, name="orders", tags=("core",)
        )
        self.stg_customers = Dataset(
            id=UUID(int=13), layer_id=self.stg.id, name="customers"
        )
        self.dm_sales = Dataset(
            id=UUID(int=14),
            layer_id=self.dm.id,
            name="sales",
            description="daily sales",
        )

        self.raw_orders_id = self._column(
            self.raw_orders, "order_id", "int", 0, key=True
        )
        self.raw_orders_customer = self._column(
            self.raw_orders, "customer_id", "int", 1, key=False
        )
        self.raw_orders_amount = self._column(
            self.raw_orders, "amount", "numeric", 2, key=False
        )
        self.raw_orders_updated = self._column(
            self.raw_orders, "updated_at", "timestamp", 3, key=False
        )
        self.raw_customers_id = self._column(
            self.raw_customers, "customer_id", "int", 0, key=True
        )
        self.raw_customers_name = self._column(
            self.raw_customers, "name", "text", 1, key=False
        )
        self.stg_orders_id = self._column(
            self.stg_orders, "order_id", "int", 0, key=True
        )
        self.stg_orders_customer = self._column(
            self.stg_orders, "customer_id", "int", 1, key=False
        )
        self.stg_orders_amount = self._column(
            self.stg_orders, "amount", "numeric", 2, key=False
        )
        self.stg_customers_id = self._column(
            self.stg_customers, "customer_id", "int", 0, key=True
        )
        self.stg_customers_name = self._column(
            self.stg_customers, "name", "text", 1, key=False
        )
        self.dm_sales_customer = self._column(
            self.dm_sales, "customer_id", "int", 0, key=True
        )
        self.dm_sales_total = self._column(
            self.dm_sales, "total", "numeric", 1, key=False
        )

        self.full = LoadKind(
            id=UUID(int=40), name="full", description="truncate and load", fields=()
        )
        self.hashkey = LoadKind(
            id=UUID(int=41),
            name="hashkey",
            fields=(
                LoadField(
                    name="hash_columns", type=LoadFieldType.COLUMNS, required=True
                ),
                LoadField(name="batch", type=LoadFieldType.INT, required=False),
            ),
        )

        self.flow_orders = Flow(
            id=UUID(int=50),
            from_dataset_id=self.raw_orders.id,
            to_dataset_id=self.stg_orders.id,
            load=LoadSpec(
                kind_id=self.hashkey.id,
                values={"hash_columns": (self.raw_orders_id.id,), "batch": 1000},
            ),
        )
        self.flow_customers = Flow(
            id=UUID(int=51),
            from_dataset_id=self.raw_customers.id,
            to_dataset_id=self.stg_customers.id,
            load=LoadSpec(kind_id=self.full.id, values={}),
        )
        self.flow_sales = Flow(
            id=UUID(int=52),
            from_dataset_id=self.stg_orders.id,
            to_dataset_id=self.dm_sales.id,
            load=LoadSpec(kind_id=self.full.id, values={}),
            description="aggregate by customer",
        )

    def _column(
        self,
        dataset: Dataset,
        name: str,
        type_: str,
        position: int,
        *,
        key: bool,
    ) -> Column:
        self._next_column_id += 1
        return Column(
            id=UUID(int=self._next_column_id),
            dataset_id=dataset.id,
            name=name,
            type=type_,
            nullable=not key,
            is_key=key,
            position=position,
        )

    def layers(self) -> tuple[Layer, ...]:
        return (self.raw, self.stg, self.dm)

    def datasets(self) -> tuple[Dataset, ...]:
        return (
            self.raw_orders,
            self.raw_customers,
            self.stg_orders,
            self.stg_customers,
            self.dm_sales,
        )

    def columns(self) -> tuple[Column, ...]:
        return (
            self.raw_orders_id,
            self.raw_orders_customer,
            self.raw_orders_amount,
            self.raw_orders_updated,
            self.raw_customers_id,
            self.raw_customers_name,
            self.stg_orders_id,
            self.stg_orders_customer,
            self.stg_orders_amount,
            self.stg_customers_id,
            self.stg_customers_name,
            self.dm_sales_customer,
            self.dm_sales_total,
        )

    def load_kinds(self) -> tuple[LoadKind, ...]:
        return (self.full, self.hashkey)

    def flows(self) -> tuple[Flow, ...]:
        return (self.flow_orders, self.flow_customers, self.flow_sales)

    def ops(self) -> list[CatalogOp]:
        ops: list[CatalogOp] = []
        for layer in self.layers():
            ops.append(AddLayer(layer=layer))

        for dataset in self.datasets():
            ops.append(AddDataset(dataset=dataset))

        for column in self.columns():
            ops.append(AddColumn(column=column))

        for kind in self.load_kinds():
            ops.append(AddLoadKind(load_kind=kind))

        for flow in self.flows():
            ops.append(AddFlow(flow=flow))

        return ops

    def snapshot(self) -> CatalogSnapshot:
        ops = OperationList(root=tuple(self.ops()))
        return ops.apply(CatalogSnapshot.empty())
