# ruff: noqa: S608, E501
"""Перелив отчёта по заказам из postgres и Greenplum в ClickHouse парой
postgres -> ClickHouse (pg_stream_out и ch_stream_in) запросом, каким его
написал бы LLM: CTE, join и left join с NULL, оконные функции, агрегаты в
строку, enum, массив, uuid, inet, interval, date_trunc.

Источник — каждый postgres (9.0–19) и Greenplum (6, 7) стенда, приёмник —
каждый ClickHouse стенда. Запрос отчёта держится возможностей 9.0 (без
lateral, filter и jsonb); JSON проверяется отдельно на источниках с 9.4.
Сценарии идут по порядку на каждой паре и опираются на таблицы предыдущих:
    - первая попытка с enum и массивом как есть: отказ, затем String по
      fallback_as_varchar;
    - загрузка отчёта с приведёнными типами, повторная с проверкой схемы;
    - инкремент одного месяца через delete_where;
    - витрина приёмника со своими именами через rename_columns и column_types;
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table с partition by, ReplicatedMergeTree on cluster там,
      где есть Keeper;
    - подмена витрины шагами after через exchange tables;
    - сухой прогон do_nothing + insert nothing;
    - json и jsonb источника в JSON или String по версии сервера;
    - поток arrow того же отчёта;
    - обратный путь: агрегат «город × месяц» из ClickHouse в postgres.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.target import ChStreamWire, ChTableRef
from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.transfer import PgColumnDeclaration, PgTransferTable
from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Leg,
    Loaded,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.toolkit.transfer import (
    BackupAndCreateIfSchemaChanged,
    ColumnRules,
    DeleteNothing,
    DeleteTruncate,
    DeleteWhere,
    DoNothing,
    DropAndCreate,
    ErrorIfNotExists,
    ErrorIfSchemaChanged,
    FailOnUnknown,
    FallbackAsVarchar,
    InsertFull,
    InsertNothing,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
SRC = "shop_pg"
DW = "shop_pg_dw"
CHUNK = 65536
CUSTOMERS = 2000
PRODUCTS = 500
ORDERS = 20000
MONTHS = 8
IPV4_IN_IPV6_SINCE = 23
JSON_SINCE = 24
NULLABLE_JSON_SINCE = 25
PG_JSON_SINCE = 90400
KEEPER_CLUSTER = "stand"
MERGE_TREE = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree order by {order_by}"
)
PARTITIONED = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree partition by toYYYYMM(month) order by {order_by}"
)
SYNC = {"mutations_sync": 2}

SCHEMA_DDL = [
    f"create type {SRC}.customer_tier as enum ('bronze', 'silver', 'gold')",
    f"create type {SRC}.order_status as enum ('new', 'paid', 'shipped', 'cancelled')",
    f"""
    create table {SRC}.customers (
        id bigint primary key,
        external_id uuid not null,
        name text not null,
        email text not null,
        tier {SRC}.customer_tier not null,
        signed_up timestamptz not null,
        city text not null,
        tags text[] not null,
        last_ip inet,
        is_active boolean not null
    )""",
    f"""
    create table {SRC}.products (
        id bigint primary key,
        sku varchar(32) not null,
        title text not null,
        category text not null,
        price numeric(12,2) not null,
        weight_kg real
    )""",
    f"""
    create table {SRC}.orders (
        id bigint primary key,
        customer_id bigint not null,
        status {SRC}.order_status not null,
        placed_at timestamptz not null,
        shipped_at timestamptz,
        note text
    )""",
    f"""
    create table {SRC}.order_items (
        id bigint primary key,
        order_id bigint not null,
        product_id bigint not null,
        qty integer not null,
        unit_price numeric(12,2) not null,
        discount numeric(5,4) not null
    )""",
    f"""
    create table {SRC}.payments (
        id bigint primary key,
        order_id bigint not null,
        amount numeric(14,2) not null,
        method text not null,
        paid_at timestamptz not null
    )""",
]

FILL_DML = [
    f"""
    insert into {SRC}.customers
    select g,
           md5(g::text)::uuid,
           'Customer ' || g,
           'user' || g || '@Example.COM',
           ((array['bronze', 'silver', 'gold'])[1 + g % 3])::{SRC}.customer_tier,
           timestamptz '2023-01-01 00:00:00+00' + (g || ' hours')::interval,
           (array['Moscow', 'Kazan', 'Perm'])[1 + g % 3],
           (array['t' || g % 5, 'vip'])[1:1 + g % 2],
           case when g % 7 = 0 then null
                else ('10.0.' || g % 256 || '.' || g % 250)::inet end,
           g % 11 <> 0
    from generate_series(1, {CUSTOMERS}) g""",
    f"""
    insert into {SRC}.products
    select g,
           'SKU-' || lpad(g::text, 6, '0'),
           'Product ' || g,
           (array['books', 'toys', 'food', 'tools'])[1 + g % 4],
           ((1 + (g * 7919) % 50000) / 100.0)::numeric(12,2),
           case when g % 9 = 0 then null else ((g * 31) % 2000 / 100.0)::real end
    from generate_series(1, {PRODUCTS}) g""",
    f"""
    insert into {SRC}.orders
    select g,
           1 + (g * 7919) % {CUSTOMERS},
           ((array['new', 'paid', 'shipped', 'cancelled'])[1 + g % 4])::{SRC}.order_status,
           timestamptz '2024-01-01 00:00:00+00' + interval '1 millisecond' * (g::bigint * 1020123),
           case when g % 3 = 0 then null
                else timestamptz '2024-01-01 00:00:00+00'
                     + interval '1 millisecond' * (g::bigint * 1020123)
                     + ((g % 96) || ' hours')::interval end,
           case when g % 5 = 0
                then E'call before "delivery", floor ' || g % 20 || E'\\t\\\\ back'
                else null end
    from generate_series(1, {ORDERS}) g""",
    f"""
    insert into {SRC}.order_items
    select row_number() over (order by x.order_id, x.k),
           x.order_id,
           x.pid,
           1 + (x.order_id + x.k) % 5,
           p.price,
           ((x.order_id + x.k) % 4) * 0.05
    from (
        select o.id as order_id, o.k, 1 + (o.id * 31 + o.k * 17) % {PRODUCTS} as pid
        from (select id, generate_series(1, 1 + (id % 4)::int) as k
              from {SRC}.orders) o
    ) x
    join {SRC}.products p on p.id = x.pid""",
    f"""
    insert into {SRC}.payments
    select row_number() over (order by o.id),
           o.id,
           case when o.id % 8 = 2 then 10.00 else s.gross end,
           (array['card', 'cash', 'sbp'])[1 + o.id % 3],
           o.placed_at + interval '15 minutes'
    from {SRC}.orders o
    join (select order_id,
                 sum(qty * unit_price * (1 - discount))::numeric(14,2) as gross
          from {SRC}.order_items
          group by order_id) s on s.order_id = o.id
    where o.status in ('paid', 'shipped')""",
    f"""
    insert into {SRC}.payments
    select {ORDERS} * 2 + id, id, 1.00, 'bonus', placed_at + interval '1 hour'
    from {SRC}.orders
    where status = 'paid' and id % 6 = 1""",
]

REPORT_SQL = f"""
with items as (
    select oi.order_id,
           count(*) as lines,
           sum(oi.qty) as units,
           sum(oi.qty * oi.unit_price * (1 - oi.discount))::numeric(14,2) as gross,
           array_to_string(array_agg(distinct p.category order by p.category), ',')
               as categories
    from {SRC}.order_items oi
    join {SRC}.products p on p.id = oi.product_id
    group by oi.order_id
),
pay as (
    select order_id,
           sum(amount)::numeric(14,2) as paid_sum,
           max(paid_at) as last_paid_at,
           string_agg(distinct method, ',' order by method) as methods
    from {SRC}.payments
    group by order_id
)
select o.id                                              as order_id,
       o.status::text                                    as status,
       c.external_id                                     as customer_uid,
       c.name                                            as customer_name,
       lower(c.email)                                    as email,
       c.tier::text                                      as tier,
       c.city,
       array_to_string(c.tags, ',')                      as tags,
       c.last_ip,
       c.is_active,
       o.placed_at,
       date_trunc('month', o.placed_at at time zone 'UTC')::date as month,
       o.shipped_at,
       o.shipped_at - o.placed_at                        as lead_time,
       coalesce(o.note, '')                              as note,
       i.lines,
       i.units,
       i.gross,
       i.categories,
       pay.paid_sum                                      as paid,
       pay.last_paid_at                                  as paid_at,
       pay.methods,
       case when pay.paid_sum is null then 'unpaid'
            when pay.paid_sum >= i.gross then 'paid'
            else 'partial' end                           as payment_state,
       (i.gross - coalesce(pay.paid_sum, 0))::numeric(14,2) as balance,
       row_number() over (partition by c.id order by o.placed_at) as customer_order_no,
       (sum(i.gross) over (partition by c.id))::numeric(16,2) as customer_total,
       (avg(i.gross) over (partition by date_trunc('month', o.placed_at at time zone 'UTC')))::float8
                                                         as month_avg
from {SRC}.orders o
join {SRC}.customers c on c.id = o.customer_id
join items i on i.order_id = o.id
left join pay on pay.order_id = o.id
"""

RAW_SQL = f"""
select o.id as order_id, o.status, c.tier, c.tags
from {SRC}.orders o
join {SRC}.customers c on c.id = o.customer_id
"""
"""Первая попытка LLM: enum и массив источника как есть."""

RAW_COLUMNS = (PgColumnDeclaration(name="order_id", nullable=False),)

DECLARED = (
    PgColumnDeclaration(name="order_id", nullable=False),
    PgColumnDeclaration(name="placed_at", nullable=False),
    PgColumnDeclaration(name="month", nullable=False),
)
"""Ключевые поля LLM объявляет not null: их берут order by и partition by."""

EXPECTED_TYPES = [
    ("order_id", "Int64"),
    ("status", "Nullable(String)"),
    ("customer_uid", "Nullable(UUID)"),
    ("customer_name", "Nullable(String)"),
    ("email", "Nullable(String)"),
    ("tier", "Nullable(String)"),
    ("city", "Nullable(String)"),
    ("tags", "Nullable(String)"),
    ("last_ip", "{inet}"),
    ("is_active", "Nullable(Bool)"),
    ("placed_at", "DateTime64(6, 'UTC')"),
    ("month", "Date32"),
    ("shipped_at", "Nullable(DateTime64(6, 'UTC'))"),
    ("lead_time", "Nullable(String)"),
    ("note", "Nullable(String)"),
    ("lines", "Nullable(Int64)"),
    ("units", "Nullable(Int64)"),
    ("gross", "Nullable(Decimal(14, 2))"),
    ("categories", "Nullable(String)"),
    ("paid", "Nullable(Decimal(14, 2))"),
    ("paid_at", "Nullable(DateTime64(6, 'UTC'))"),
    ("methods", "Nullable(String)"),
    ("payment_state", "Nullable(String)"),
    ("balance", "Nullable(Decimal(14, 2))"),
    ("customer_order_no", "Nullable(Int64)"),
    ("customer_total", "Nullable(Decimal(16, 2))"),
    ("month_avg", "Nullable(Float64)"),
]
"""Колонки таблицы ClickHouse: всё, что сервер postgres описал как выборку,
Nullable, кроме объявленных; inet — IPv6 там, где TabSeparated читает текст
IPv4 в IPv6, иначе String."""

COMPARED: Mapping[str, tuple[str, str]] = {
    "order_id": ("{c}", "{c}"),
    "last_ip": ("host({c})", "replaceOne(toString({c}), '::ffff:', '')"),
    "placed_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')",
        "toString({c})",
    ),
    "shipped_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')",
        "toString({c})",
    ),
    "paid_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')",
        "toString({c})",
    ),
    "gross": ("{c}", "{c}"),
    "paid": ("{c}", "{c}"),
    "balance": ("{c}", "{c}"),
    "customer_total": ("{c}", "{c}"),
    "month_avg": ("{c}", "{c}"),
}
"""Как сравнить колонку: выражение postgres и выражение ClickHouse с
одинаковым значением. Числа — значением, время — текстом в UTC, inet —
адресом без маски и без префикса ::ffff: у IPv6; остальные — ::text против
toString."""

FLOAT_COLUMNS = ("month_avg",)


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def source(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, SRC)
    await side.connect()
    await side.recreate_schema(SCHEMA_DDL)
    await side.execute(FILL_DML)
    await side.execute(
        [
            f"create view {SRC}.orders_report as {REPORT_SQL}",
            f"create view {SRC}.raw_orders as {RAW_SQL}",
        ]
    )

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def target(request: Any, source: PostgresSide) -> AsyncIterator[ClickHouseSide]:
    """База приёмника пересоздаётся на каждый источник: сценарии опираются
    на таблицы предыдущих."""
    side = ClickHouseSide(request.param, DW)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


def expected_types(target: ClickHouseSide) -> list[tuple[str, str]]:
    inet = "Nullable(IPv6)"
    if target.major < IPV4_IN_IPV6_SINCE:
        inet = "Nullable(String)"

    types: list[tuple[str, str]] = []
    for name, kind in EXPECTED_TYPES:
        types.append((name, kind.format(inet=inet)))

    return types


async def transfer(  # noqa: PLR0913
    source: PostgresSide,
    target: ClickHouseSide,
    table: str,
    schema: Any,
    delete: Any,
    select: str = REPORT_SQL,
    columns: Sequence[PgColumnDeclaration] = DECLARED,
    rules: ColumnRules = ColumnRules(),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
    insert: Any = InsertFull(kind="full"),
    create_table: str = MERGE_TREE,
    order_by: str = "order_id",
    cluster: str = "",
    after: Sequence[str] = (),
    wire: StreamWire = StreamWire.TSV,
) -> str:
    """pg_stream_out на source -> ch_stream_in на target."""
    pumps = Pumps(postgres=source.profile, clickhouse=target.profile)
    chained = await pumps.chain(
        Leg(
            "pg_stream_out",
            {
                "sql": select,
                "wire": wire,
                "columns": columns,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
        Leg(
            "ch_stream_in",
            {
                "database": DW,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "unknown_types": unknown_types,
                "cluster": cluster,
                "order_by": order_by,
                "create_table": create_table,
                "after": after,
            },
        ),
    )
    print(
        f"\n--- {source.source.name} -> {target.source.name}: pg_stream_out "
        f"({wire.value}) ---\n{chained.out_report}"
    )
    print(f"--- ch_stream_in ---\n{chained.in_report}")

    return chained.in_report


def month_of(month: str) -> str:
    """Выборка отчёта за один месяц: окна посчитаны по всему отчёту."""
    return f"select * from ({REPORT_SQL}) r where r.month = date '{month}'"


async def same_content(
    source: PostgresSide, target: ClickHouseSide, table: str
) -> None:
    """Каждая колонка таблицы ClickHouse совпадает с отчётом источника."""
    report = Loaded(source, SRC, "orders_report")
    landed = ChLoaded(target, table)
    for name, _ in EXPECTED_TYPES:
        pg_template, ch_template = COMPARED.get(name, ("{c}::text", "toString({c})"))
        expected = await report.scalars(pg_template.format(c=name), order_by="order_id")
        actual = await landed.column(ch_template.format(c=name), order_by="order_id")
        if name in FLOAT_COLUMNS:
            for got, want in zip(actual, expected, strict=True):
                assert abs(got - want) <= abs(want) * 1e-12, f"column {name} differs"

            continue

        assert actual == expected, f"column {name} differs"


class TestFirstAttempt:
    """LLM выгружает enum и массив как есть: у ClickHouse пары для них нет."""

    async def test_enum_is_refused(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        with pytest.raises(TransferError, match="column status: "):
            await transfer(
                source,
                target,
                "raw_orders",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                select=RAW_SQL,
                columns=RAW_COLUMNS,
            )

    async def test_fallback_lands_strings(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "raw_orders",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=RAW_SQL,
            columns=RAW_COLUMNS,
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        landed = ChLoaded(target, "raw_orders")
        view = Loaded(source, SRC, "raw_orders")

        assert f"{ORDERS} rows written" in report
        assert await landed.types() == [
            ("order_id", "Int64"),
            ("status", "Nullable(String)"),
            ("tier", "Nullable(String)"),
            ("tags", "Nullable(String)"),
        ]
        for name in ("status", "tier", "tags"):
            assert await landed.column(name, order_by="order_id") == (
                await view.texts(name, order_by="order_id")
            ), f"column {name} differs"


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
        )
        landed = ChLoaded(target, "orders_report")

        assert f"{ORDERS} rows written" in report
        assert await landed.types() == expected_types(target)
        assert await landed.count() == ORDERS
        assert await landed.scalar("countIf(paid is null)") > 0
        assert await landed.scalar("countIf(position(note, '\\t') > 0)") > 0
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
        )

        assert "error" not in report.split("rows written")[0].lower()
        assert await ChLoaded(target, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    """Перезаливка одного месяца: delete_where переносит в двойник строки
    вне месяца, insert full кладёт свежие строки месяца, exchange tables
    подменяет таблицу."""

    MONTH: ClassVar[str] = "2024-03-01"

    async def test_one_month_is_replaced(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        await target.command(
            f"alter table {DW}.orders_report update note = 'stale' "
            f"where month = toDate32('{self.MONTH}')",
            settings=SYNC,
        )
        in_month = await Loaded(source, SRC, "orders_report").aggregate(
            f"count(case when month = date '{self.MONTH}' then 1 end)"
        )

        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(kind="delete_where", where=f"month = toDate32('{self.MONTH}')"),
            select=month_of(self.MONTH),
        )

        assert in_month > 0
        assert f"{in_month} rows written" in report
        assert await landed.scalar("countIf(note = 'stale')") == 0
        assert await landed.count() == ORDERS
        await same_content(source, target, "orders_report")


class TestRenamedMart:
    """Витрина ClickHouse со своими именами и типами: rename_columns
    связывает колонки витрины с полями потока, column_types даёт
    LowCardinality, объявления источника снимают Nullable с ключевых полей;
    Decimal витрины шире потока — предупреждение."""

    MART: ClassVar[str] = f"""
        create table {DW}.orders_mart (
            order_uid Int64,
            client String,
            client_tier LowCardinality(String),
            paid_amount Nullable(Decimal(18, 2)),
            gross Nullable(Decimal(18, 2)),
            placed_at DateTime64(6, 'UTC')
        ) engine = MergeTree order by order_uid"""
    SELECT: ClassVar[str] = (
        f"select order_id, customer_name, tier, paid, gross, placed_at "
        f"from ({REPORT_SQL}) r"
    )
    COLUMNS: ClassVar[tuple[PgColumnDeclaration, ...]] = (
        PgColumnDeclaration(name="order_id", nullable=False),
        PgColumnDeclaration(name="customer_name", nullable=False),
        PgColumnDeclaration(name="tier", nullable=False),
        PgColumnDeclaration(name="placed_at", nullable=False),
    )
    RULES: ClassVar[ColumnRules] = ColumnRules(
        rename_columns={
            "order_uid": "order_id",
            "client": "customer_name",
            "client_tier": "tier",
            "paid_amount": "paid",
        },
        column_types={"client_tier": "LowCardinality(String)"},
    )

    async def test_missing_mart_is_refused(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        with pytest.raises(TransferError, match="table is missing"):
            await transfer(
                source,
                target,
                "orders_mart",
                ErrorIfNotExists(kind="error_if_not_exists"),
                DeleteNothing(kind="nothing"),
                select=self.SELECT,
                columns=self.COLUMNS,
                rules=self.RULES,
                order_by="order_uid",
            )

    async def test_mart_is_filled_by_its_own_names(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        await target.command(f"drop table if exists {DW}.orders_mart")
        await target.command(self.MART)
        view = Loaded(source, SRC, "orders_report")
        mart = ChLoaded(target, "orders_mart")

        report = await transfer(
            source,
            target,
            "orders_mart",
            ErrorIfNotExists(kind="error_if_not_exists"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            columns=self.COLUMNS,
            rules=self.RULES,
            order_by="order_uid",
        )

        assert f"{ORDERS} rows written" in report
        assert re.search(r"warning column gross: .*Decimal\(18, 2\).* is wider", report)
        assert await mart.count() == ORDERS
        assert await mart.column("client", order_by="order_uid") == (
            await view.texts("customer_name", order_by="order_id")
        )
        assert await mart.column("client_tier", order_by="order_uid") == (
            await view.texts("tier", order_by="order_id")
        )
        assert await mart.scalar("sum(paid_amount)") == await view.aggregate(
            "sum(paid)"
        )


class TestSchemaDrift:
    """Таблица ClickHouse разошлась с потоком: error_if_schema_changed
    отказывает с текстом расхождения, backup_and_create_if_schema_changed
    переименовывает её в _bak_<время> и создаёт заново."""

    async def test_drift_is_refused_then_backed_up(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        drift = ChLoaded(target, "orders_drift")
        await transfer(
            source,
            target,
            "orders_drift",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
        )
        await target.command(
            f"alter table {DW}.orders_drift modify column lines Nullable(Int32)",
            settings=SYNC,
        )

        with pytest.raises(TransferError, match="lines: "):
            await transfer(
                source,
                target,
                "orders_drift",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                DeleteTruncate(kind="truncate"),
            )

        report = await transfer(
            source,
            target,
            "orders_drift",
            BackupAndCreateIfSchemaChanged(kind="backup_and_create_if_schema_changed"),
            DeleteNothing(kind="nothing"),
        )
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("orders_drift_bak_"):
                backups.append(name)

        assert "backup: orders_drift_bak_" in report
        assert len(backups) == 1
        assert await ChLoaded(target, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("lines", "Nullable(Int64)") in await drift.types()


class TestCreateTemplate:
    """Шаблон create table: партиции по месяцу и составной ключ сортировки;
    на сервере с Keeper — ReplicatedMergeTree шаблона по умолчанию on
    cluster."""

    async def test_partitioned_by_month(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        for _ in range(2):
            report = await transfer(
                source,
                target,
                "orders_tpl",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                create_table=PARTITIONED,
                order_by="(month, order_id)",
            )

            assert f"{ORDERS} rows written" in report

        landed = ChLoaded(target, "orders_tpl")

        assert await landed.sorting_key() == "month, order_id"
        assert await landed.scalar("uniqExact(_partition_id)") == MONTHS
        assert await landed.count() == ORDERS

    async def test_replicated_on_cluster(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_replicated")
        if KEEPER_CLUSTER not in await landed.clusters():
            pytest.skip("the server has no Keeper cluster")

        report = await transfer(
            source,
            target,
            "orders_replicated",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            create_table=ChTableRef.CREATE_TABLE,
            cluster=KEEPER_CLUSTER,
        )

        assert f"{ORDERS} rows written" in report
        assert await landed.engine() == "ReplicatedMergeTree"
        assert await landed.count() == ORDERS


class TestAfterSwap:
    """Загрузка в staging и подмена витрины шагом after exchange tables.
    У ClickHouse нет транзакции: шаг after идёт после загрузки отдельно."""

    SWAP: ClassVar[tuple[str, ...]] = (
        f"exchange tables {DW}.orders_report and {DW}.orders_report_stage",
        f"drop table {DW}.orders_report_stage",
    )

    async def test_swap_replaces_the_mart(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        await target.command(
            f"alter table {DW}.orders_report update note = 'old' where 1",
            settings=SYNC,
        )

        report = await transfer(
            source,
            target,
            "orders_report_stage",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            after=self.SWAP,
        )

        assert f"{ORDERS} rows written" in report
        assert "orders_report_stage" not in await landed.tables()
        assert await landed.scalar("countIf(note = 'old')") == 0
        await same_content(source, target, "orders_report")


class TestDryRun:
    """do_nothing и insert nothing: поток прочитан до конца, таблица не
    тронута ни схемой, ни данными."""

    async def test_nothing_changes(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        types = await landed.types()

        report = await transfer(
            source,
            target,
            "orders_report",
            DoNothing(kind="do_nothing"),
            DeleteNothing(kind="nothing"),
            insert=InsertNothing(kind="nothing"),
        )

        assert report.startswith("0 rows written")
        assert await landed.types() == types
        assert await landed.count() == ORDERS


class TestJson:
    """Профиль клиента jsonb и json источника: JSON на ClickHouse 24.x+
    (Object('json') на 22/23), nullable json — Nullable(JSON) только с 25.x,
    иначе String. Источники до 9.4 json_build_object не умеют."""

    SELECT: ClassVar[str] = f"""
        select c.id as customer_id,
               json_build_object('city', c.city, 'tier', c.tier::text,
                                 'tags', array_to_json(c.tags))::jsonb as profile,
               case when c.last_ip is null then null
                    else json_build_object('ip', host(c.last_ip)) end as net
        from {SRC}.customers c"""
    COLUMNS: ClassVar[tuple[PgColumnDeclaration, ...]] = (
        PgColumnDeclaration(name="customer_id", nullable=False),
        PgColumnDeclaration(name="profile", nullable=False),
    )

    def types(self, target: ClickHouseSide) -> list[tuple[str, str]]:
        profile = "JSON"
        if target.major < JSON_SINCE:
            profile = "Object('json')"

        net = "Nullable(JSON)"
        if target.major < NULLABLE_JSON_SINCE:
            net = "Nullable(String)"

        return [("customer_id", "Int64"), ("profile", profile), ("net", net)]

    async def test_profiles_land_as_json(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        if source.version < PG_JSON_SINCE:
            pytest.skip("json_build_object and jsonb appear in 9.4")

        report = await transfer(
            source,
            target,
            "profiles",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            columns=self.COLUMNS,
            order_by="customer_id",
        )
        landed = ChLoaded(target, "profiles")
        profiles = await source.select(
            "customers",
            [
                "json_build_object('city', city, 'tier', tier::text, "
                "'tags', array_to_json(tags))::text"
            ],
        )

        assert f"{CUSTOMERS} rows written" in report
        assert await landed.types() == self.types(target)
        assert await landed.count() == CUSTOMERS
        if target.major < JSON_SINCE:
            return

        landed_profiles: list[Any] = []
        for text in await landed.column("toString(profile)", order_by="customer_id"):
            landed_profiles.append(json.loads(text))

        source_profiles: list[Any] = []
        for row in profiles:
            source_profiles.append(json.loads(row[0]))

        assert landed_profiles == source_profiles


class TestArrow:
    """Тот же отчёт потоком arrow: контракт нейтральный, приёмник строит
    типы ClickHouse по Arrow; суммы и тексты совпадают с отчётом."""

    SELECT: ClassVar[str] = (
        f"select order_id, status, customer_uid, customer_name, city, month, "
        f"placed_at, gross, paid, is_active from ({REPORT_SQL}) r"
    )

    async def test_arrow_lands_the_report(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report_arrow",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            wire=StreamWire.ARROW,
        )
        landed = ChLoaded(target, "orders_report_arrow")
        view = Loaded(source, SRC, "orders_report")

        assert f"{ORDERS} rows written" in report
        assert await landed.count() == ORDERS
        assert await landed.scalar("sum(gross)") == await view.aggregate("sum(gross)")
        assert await landed.scalar("sum(paid)") == await view.aggregate("sum(paid)")
        for name in ("status", "customer_uid", "customer_name", "city", "month"):
            assert await landed.column(f"toString({name})", order_by="order_id") == (
                await view.texts(name, order_by="order_id")
            ), f"column {name} differs"


class TestBackToPostgres:
    """Обратный путь: агрегат «город × месяц» из ClickHouse в схему
    источника парой ClickHouse -> postgres; суммы совпадают с отчётом."""

    AGGREGATE: ClassVar[str] = (
        f"select city, month, count() as orders, sum(gross) as gross, "
        f"sum(balance) as balance from {DW}.orders_report group by city, month"
    )

    async def test_city_month_comes_back(
        self, source: PostgresSide, target: ClickHouseSide
    ) -> None:
        pumps = Pumps(postgres=source.profile, clickhouse=target.profile)
        chained = await pumps.chain(
            Leg(
                "ch_stream_out",
                {"sql": self.AGGREGATE, "wire": ChStreamWire.TSV, "chunk_bytes": CHUNK},
            ),
            Leg(
                "pg_stream_in",
                {
                    "schema_name": SRC,
                    "table_name": "city_month",
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "rules": ColumnRules(),
                    "create_table": PgTransferTable.CREATE_TABLE,
                    "copy_options": CopyOptions(chunk_bytes=CHUNK),
                },
            ),
        )
        print(f"\n--- {target.source.name} -> {source.source.name} ---")
        print(chained.in_report)
        city_month = Loaded(source, SRC, "city_month")
        view = Loaded(source, SRC, "orders_report")

        assert await city_month.columns() == [
            ("city", "text", False),
            ("month", "date", True),
            ("orders", "numeric(20,0)", True),
            ("gross", "numeric(38,2)", False),
            ("balance", "numeric(38,2)", False),
        ]
        assert await city_month.aggregate("sum(orders)") == ORDERS
        assert await city_month.aggregate("sum(gross)") == await view.aggregate(
            "sum(gross)"
        )
        assert await city_month.aggregate("sum(balance)") == await view.aggregate(
            "sum(balance)"
        )


async def test_rows_reached_every_column(source: PostgresSide) -> None:
    """Сторож данных стенда: в отчёте есть и NULL левого join, и частичные
    оплаты, и заметки с табуляцией — иначе сценарии выше ничего не ловят."""
    view = Loaded(source, SRC, "orders_report")

    assert await view.aggregate("count(case when paid is null then 1 end)") > 0
    assert (
        await view.aggregate("count(case when payment_state = 'partial' then 1 end)")
        > 0
    )
    assert await view.aggregate("count(case when note like E'%\\t%' then 1 end)") > 0
    assert await view.aggregate("count(case when last_ip is null then 1 end)") > 0
    assert await view.count() == ORDERS
