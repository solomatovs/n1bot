"""Образцы снимков источников для тестов, стендов и показа: Postgres с
секционированной таблицей, представлением, функцией с перегрузкой и
процедурой; ClickHouse с таблицей MergeTree, представлениями и словарём."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from boba.catalog.clickhouse_snapshot import (
    ChColumn,
    ChDatabase,
    ChDictionary,
    ChDictionaryAttribute,
    ChSnapshot,
    ChTable,
    ChTableKind,
)
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
    ObjectResolver,
)
from boba.catalog.ops import AddFlow, AddLayer, AddLoadKind, AddNode, OperationList
from boba.catalog.postgres_snapshot import (
    PgColumn,
    PgConstraint,
    PgConstraintKind,
    PgDatabase,
    PgIndex,
    PgRelation,
    PgRelationKind,
    PgRoutine,
    PgRoutineArg,
    PgRoutineKind,
    PgSchema,
    PgSequence,
    PgSnapshot,
    PgType,
    PgTypeKind,
)
from boba.catalog.sources import ObjectKind, ObjectRef

__all__ = ["ChSample", "PgSample", "ProcessSample", "SampleIds", "SampleResolver"]


class SampleIds:
    """Идентификаторы источников образцов."""

    POSTGRES = UUID(int=0x5001)
    CLICKHOUSE = UUID(int=0x5002)


class PgSample:
    """База prod, схемы public и etl."""

    def __init__(self) -> None:
        self.database = PgDatabase(name="prod", owner="postgres", encoding="UTF8")
        self.public = PgSchema(database="prod", name="public", owner="postgres")
        self.etl = PgSchema(
            database="prod", name="etl", owner="etl", comment="Загрузка"
        )

        self.orders = PgRelation(
            database="prod",
            schema_name="public",
            name="orders",
            kind=PgRelationKind.PARTITIONED,
            owner="app",
            comment="Заказы",
            row_estimate=1_200_000,
            total_bytes=512_000_000,
            partition_key="RANGE (created_at)",
        )
        self.orders_2026 = PgRelation(
            database="prod",
            schema_name="public",
            name="orders_2026",
            kind=PgRelationKind.PARTITION,
            owner="app",
            partition_of="public.orders",
            partition_bound="FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
        )
        self.customers = PgRelation(
            database="prod",
            schema_name="public",
            name="customers",
            kind=PgRelationKind.TABLE,
            owner="app",
        )
        self.v_orders = PgRelation(
            database="prod",
            schema_name="public",
            name="v_orders",
            kind=PgRelationKind.VIEW,
            definition="SELECT id, amount FROM public.orders",
        )

        self.orders_id = PgColumn(
            database="prod",
            schema_name="public",
            relation="orders",
            name="id",
            ordinal=1,
            type="bigint",
            nullable=False,
            identity="always",
        )
        self.orders_amount = PgColumn(
            database="prod",
            schema_name="public",
            relation="orders",
            name="amount",
            ordinal=2,
            type="numeric(10,2)",
            nullable=False,
            comment="Сумма",
        )
        self.orders_created = PgColumn(
            database="prod",
            schema_name="public",
            relation="orders",
            name="created_at",
            ordinal=3,
            type="timestamp with time zone",
            nullable=False,
            default="now()",
        )
        self.customers_id = PgColumn(
            database="prod",
            schema_name="public",
            relation="customers",
            name="id",
            ordinal=1,
            type="integer",
            nullable=False,
        )
        self.v_orders_id = PgColumn(
            database="prod",
            schema_name="public",
            relation="v_orders",
            name="id",
            ordinal=1,
            type="bigint",
        )

        self.orders_pk = PgConstraint(
            database="prod",
            schema_name="public",
            relation="orders",
            name="orders_pkey",
            kind=PgConstraintKind.PRIMARY,
            columns=("id", "created_at"),
            definition="PRIMARY KEY (id, created_at)",
        )
        self.orders_idx = PgIndex(
            database="prod",
            schema_name="public",
            relation="orders",
            name="orders_created_idx",
            columns=("created_at",),
            definition=(
                "CREATE INDEX orders_created_idx ON public.orders"
                " USING btree (created_at)"
            ),
        )

        self.load_orders = PgRoutine(
            database="prod",
            schema_name="etl",
            name="load_orders",
            signature="date",
            kind=PgRoutineKind.PROCEDURE,
            language="plpgsql",
            arguments="day date",
            body=(
                "BEGIN INSERT INTO public.orders SELECT * FROM stage.orders"
                " WHERE d = day; END"
            ),
            definition="CREATE PROCEDURE etl.load_orders(day date) ...",
        )
        self.hash_one = PgRoutine(
            database="prod",
            schema_name="etl",
            name="hash_key",
            signature="text",
            kind=PgRoutineKind.FUNCTION,
            language="sql",
            arguments="value text",
            returns="text",
            volatility="immutable",
            body="SELECT md5(value)",
        )
        self.hash_two = PgRoutine(
            database="prod",
            schema_name="etl",
            name="hash_key",
            signature="text, text",
            kind=PgRoutineKind.FUNCTION,
            language="sql",
            arguments="a text, b text",
            returns="text",
            volatility="immutable",
            body="SELECT md5(a || b)",
        )
        self.load_orders_arg = PgRoutineArg(
            database="prod",
            schema_name="etl",
            routine="load_orders",
            signature="date",
            position=0,
            name="day",
            type="date",
        )
        self.orders_seq = PgSequence(
            database="prod",
            schema_name="public",
            name="orders_id_seq",
            owned_by="public.orders.id",
        )
        self.status_type = PgType(
            database="prod",
            schema_name="public",
            name="order_status",
            kind=PgTypeKind.ENUM,
            labels=("new", "paid", "shipped"),
        )

    def snapshot(self) -> PgSnapshot:
        return PgSnapshot(
            databases=(self.database,),
            schemas=(self.public, self.etl),
            relations=(self.orders, self.orders_2026, self.customers, self.v_orders),
            columns=(
                self.orders_id,
                self.orders_amount,
                self.orders_created,
                self.customers_id,
                self.v_orders_id,
            ),
            constraints=(self.orders_pk,),
            indexes=(self.orders_idx,),
            routines=(self.load_orders, self.hash_one, self.hash_two),
            routine_args=(self.load_orders_arg,),
            sequences=(self.orders_seq,),
            types=(self.status_type,),
        )

    def next_version(self) -> PgSnapshot:
        """Следующая версия: customers удалена, у orders.amount новый тип и
        добавлена колонка note, у процедуры новое тело, появилась таблица."""
        amount = self.orders_amount.model_copy(update={"type": "numeric(12,2)"})
        note = PgColumn(
            database="prod",
            schema_name="public",
            relation="orders",
            name="note",
            ordinal=4,
            type="text",
        )
        load_orders = self.load_orders.model_copy(
            update={"body": "BEGIN CALL etl.load_orders_v2(day); END"}
        )
        returns = PgRelation(
            database="prod",
            schema_name="public",
            name="returns",
            kind=PgRelationKind.TABLE,
            owner="app",
        )
        base = self.snapshot()
        return base.model_copy(
            update={
                "relations": (self.orders, self.orders_2026, self.v_orders, returns),
                "columns": (
                    self.orders_id,
                    amount,
                    self.orders_created,
                    note,
                    self.v_orders_id,
                ),
                "routines": (load_orders, self.hash_one, self.hash_two),
            }
        )


class ChSample:
    """База dwh: таблица MergeTree, представление, материализованное, словарь."""

    def __init__(self) -> None:
        self.database = ChDatabase(name="dwh", engine="Atomic")
        self.events = ChTable(
            database="dwh",
            name="events",
            kind=ChTableKind.TABLE,
            engine="MergeTree",
            engine_full="MergeTree ORDER BY (ts, user_id)",
            partition_key="toYYYYMM(ts)",
            sorting_key="ts, user_id",
            primary_key="ts, user_id",
            total_rows=10_000_000,
            total_bytes=2_000_000_000,
            create_query="CREATE TABLE dwh.events (...) ENGINE = MergeTree",
        )
        self.events_daily = ChTable(
            database="dwh",
            name="events_daily",
            kind=ChTableKind.MATERIALIZED,
            engine="MaterializedView",
            definition=(
                "SELECT toDate(ts) AS day, count() AS n FROM dwh.events GROUP BY day"
            ),
            target="dwh.events_daily_data",
        )
        self.v_events = ChTable(
            database="dwh",
            name="v_events",
            kind=ChTableKind.VIEW,
            engine="View",
            definition="SELECT * FROM dwh.events WHERE user_id > 0",
        )
        self.events_ts = ChColumn(
            database="dwh",
            table="events",
            name="ts",
            position=1,
            type="DateTime64(3)",
            in_sorting_key=True,
            in_primary_key=True,
            in_partition_key=True,
        )
        self.events_user = ChColumn(
            database="dwh",
            table="events",
            name="user_id",
            position=2,
            type="UInt64",
            in_sorting_key=True,
            in_primary_key=True,
        )
        self.events_payload = ChColumn(
            database="dwh",
            table="events",
            name="payload",
            position=3,
            type="String",
            codec="ZSTD(3)",
            comment="JSON события",
        )
        self.users_dict = ChDictionary(
            database="dwh",
            name="users",
            status="LOADED",
            layout="Hashed",
            source="ClickHouse(dwh.users_src)",
            key_columns=("user_id",),
            lifetime_min=300,
            lifetime_max=600,
        )
        self.users_name = ChDictionaryAttribute(
            database="dwh", dictionary="users", name="name", position=1, type="String"
        )

    def snapshot(self) -> ChSnapshot:
        return ChSnapshot(
            databases=(self.database,),
            tables=(self.events, self.events_daily, self.v_events),
            columns=(self.events_ts, self.events_user, self.events_payload),
            dictionaries=(self.users_dict,),
            dictionary_attributes=(self.users_name,),
        )


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


class SampleResolver(ObjectResolver):
    """Резолвер по снимку Postgres образца: объекты и колонки как в нём."""

    def __init__(self, snapshot: PgSnapshot) -> None:
        self._snapshot = snapshot

    def exists(self, ref: ObjectRef) -> bool:
        if ref.kind is ObjectKind.RELATION:
            return self._snapshot.relation(ref.path) is not None

        if ref.kind is ObjectKind.ROUTINE:
            return self._snapshot.routine(ref.path) is not None

        return False

    def columns_of(self, ref: ObjectRef) -> Sequence[str] | None:
        if ref.kind is not ObjectKind.RELATION:
            return None

        if self._snapshot.relation(ref.path) is None:
            return None

        names: list[str] = []
        for column in self._snapshot.columns_of(ref.path):
            names.append(column.name)

        return names
