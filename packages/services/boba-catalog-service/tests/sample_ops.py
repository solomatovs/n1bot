"""Образец каталога для тестов сервиса: два слоя, три набора, два вида, два потока."""

from __future__ import annotations

from uuid import UUID

from boba.catalog import (
    AddColumn,
    AddDataset,
    AddFlow,
    AddLayer,
    AddLoadKind,
    CatalogOp,
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
    """Каталог raw -> stg с детерминированными id."""

    def __init__(self) -> None:
        self.raw = Layer(id=UUID(int=101), name="raw")
        self.stg = Layer(id=UUID(int=102), name="stg")

        self.raw_orders = Dataset(
            id=UUID(int=110), layer_id=self.raw.id, name="orders", source="erp"
        )
        self.raw_items = Dataset(id=UUID(int=111), layer_id=self.raw.id, name="items")
        self.stg_orders = Dataset(
            id=UUID(int=112),
            layer_id=self.stg.id,
            name="orders",
            tags=("core", "daily"),
        )

        self.raw_orders_id = Column(
            id=UUID(int=120),
            dataset_id=self.raw_orders.id,
            name="order_id",
            type="int",
            nullable=False,
            is_key=True,
            position=0,
        )
        self.raw_orders_amount = Column(
            id=UUID(int=121),
            dataset_id=self.raw_orders.id,
            name="amount",
            type="numeric",
            nullable=True,
            is_key=False,
            position=1,
        )
        self.raw_items_id = Column(
            id=UUID(int=122),
            dataset_id=self.raw_items.id,
            name="item_id",
            type="int",
            nullable=False,
            is_key=True,
            position=0,
        )
        self.stg_orders_id = Column(
            id=UUID(int=123),
            dataset_id=self.stg_orders.id,
            name="order_id",
            type="int",
            nullable=False,
            is_key=True,
            position=0,
            description="business key",
        )

        self.full = LoadKind(id=UUID(int=130), name="full", fields=())
        self.hashkey = LoadKind(
            id=UUID(int=131),
            name="hashkey",
            fields=(
                LoadField(
                    name="hash_columns", type=LoadFieldType.COLUMNS, required=True
                ),
                LoadField(name="key", type=LoadFieldType.COLUMN, required=False),
                LoadField(name="batch", type=LoadFieldType.INT, required=False),
            ),
        )

        self.flow_orders = Flow(
            id=UUID(int=140),
            from_dataset_id=self.raw_orders.id,
            to_dataset_id=self.stg_orders.id,
            load=LoadSpec(
                kind_id=self.hashkey.id,
                values={
                    "hash_columns": (self.raw_orders_id.id, self.raw_orders_amount.id),
                    "key": self.stg_orders_id.id,
                    "batch": 500,
                },
            ),
        )
        self.flow_items = Flow(
            id=UUID(int=141),
            from_dataset_id=self.raw_items.id,
            to_dataset_id=self.stg_orders.id,
            load=LoadSpec(kind_id=self.full.id, values={}),
            description="lookup",
        )

    def ops(self) -> OperationList:
        ops: list[CatalogOp] = [
            AddLayer(layer=self.raw),
            AddLayer(layer=self.stg),
            AddDataset(dataset=self.raw_orders),
            AddDataset(dataset=self.raw_items),
            AddDataset(dataset=self.stg_orders),
            AddColumn(column=self.raw_orders_id),
            AddColumn(column=self.raw_orders_amount),
            AddColumn(column=self.raw_items_id),
            AddColumn(column=self.stg_orders_id),
            AddLoadKind(load_kind=self.full),
            AddLoadKind(load_kind=self.hashkey),
            AddFlow(flow=self.flow_orders),
            AddFlow(flow=self.flow_items),
        ]

        return OperationList(root=tuple(ops))
