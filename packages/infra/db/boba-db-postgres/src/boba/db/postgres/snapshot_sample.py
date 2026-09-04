"""Образец снимка Postgres для тестов, стендов и показа: база prod со
схемами public и etl, секционированная таблица, представление, функция с
перегрузкой и процедура; вторая версия с изменениями для diff и устаревания."""

from __future__ import annotations

from boba.db.postgres.snapshot import (
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

__all__ = ["PgSample"]


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
