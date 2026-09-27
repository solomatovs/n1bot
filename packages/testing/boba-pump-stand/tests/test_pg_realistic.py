# ruff: noqa: S608, E501
"""Перелив отчёта по заказам между двумя серверами postgres запросом, каким
его написал бы LLM: CTE, join и left join с NULL, lateral, оконные функции,
агрегаты в массив и jsonb, enum, uuid, inet, interval, date_trunc. Источник
— новейший postgres стенда, приёмник — другой сервер (предыдущий postgres и
Greenplum 7). Запускать из launch.json «pytest: текущий файл»; отчёты насосов
печатаются, видны с -s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any

import pytest

from boba.db.postgres.connection import CopyOptions
from boba.pump_stand import Landing, Leg, PostgresSide, Pumps, PumpStand
from boba.toolkit.sync import (
    ColumnDeclaration,
    ColumnRules,
    CreateIfNotExists,
    DeleteNothing,
    DeleteTruncate,
    Engine,
    ErrorIfSchemaChanged,
    InsertFull,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
SRC = "shop"
DW = "shop_dw"
CHUNK = 65536
CUSTOMERS = 2000
PRODUCTS = 500
ORDERS = 20000
TARGET_NAMES = ("pg-16", "gp-7")


def _sources_by_name() -> dict[str, Any]:
    return {source.name: source for source in STAND.sources}


def _newest_postgres() -> Any:
    newest: Any = None
    for source in STAND.sources:
        if source.name.startswith("pg-"):
            newest = source

    if newest is None:
        raise AssertionError("no postgres source on the stand")

    return newest


def _targets() -> list[Any]:
    by_name = _sources_by_name()
    chosen: list[Any] = []
    for name in TARGET_NAMES:
        if name in by_name:
            chosen.append(by_name[name])

    return chosen


SCHEMA_DDL = [
    "create type customer_tier as enum ('bronze', 'silver', 'gold')",
    "create type order_status as enum ('new', 'paid', 'shipped', 'cancelled')",
    f"""
    create table {SRC}.customers (
        id bigint primary key,
        external_id uuid not null,
        name text not null,
        email text not null,
        tier customer_tier not null,
        signed_up timestamptz not null,
        address jsonb not null,
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
        weight_kg real,
        attrs jsonb not null
    )""",
    f"""
    create table {SRC}.orders (
        id bigint primary key,
        customer_id bigint not null references {SRC}.customers (id),
        status order_status not null,
        placed_at timestamptz not null,
        shipped_at timestamptz,
        note text
    )""",
    f"""
    create table {SRC}.order_items (
        id bigint primary key,
        order_id bigint not null references {SRC}.orders (id),
        product_id bigint not null references {SRC}.products (id),
        qty integer not null,
        unit_price numeric(12,2) not null,
        discount numeric(5,4) not null
    )""",
    f"""
    create table {SRC}.payments (
        id bigint primary key,
        order_id bigint not null references {SRC}.orders (id),
        amount numeric(14,2) not null,
        method text not null,
        paid_at timestamptz not null,
        meta jsonb
    )""",
]

FILL_DML = [
    "select setseed(0.42)",
    f"""
    insert into {SRC}.customers
    select g,
           md5(g::text)::uuid,
           'Customer ' || g,
           'user' || g || '@Example.COM',
           (array['bronze', 'silver', 'gold'])[1 + g % 3]::customer_tier,
           timestamptz '2023-01-01' + (g || ' hours')::interval,
           jsonb_build_object('city', (array['Moscow', 'Kazan', 'Perm'])[1 + g % 3],
                              'geo', jsonb_build_object('lat', 55.75 + g / 1000.0)),
           (array['t' || g % 5, 'vip'])[1:1 + g % 2],
           case when g % 7 = 0 then null else ('10.0.' || g % 256 || '.' || g % 250)::inet end,
           g % 11 <> 0
    from generate_series(1, {CUSTOMERS}) g""",
    f"""
    insert into {SRC}.products
    select g,
           'SKU-' || lpad(g::text, 6, '0'),
           'Product ' || g,
           (array['books', 'toys', 'food', 'tools'])[1 + g % 4],
           (random() * 500)::numeric(12,2) + 1,
           case when g % 9 = 0 then null else (random() * 20)::real end,
           jsonb_build_object('color', (array['red', 'blue'])[1 + g % 2], 'stock', g % 100)
    from generate_series(1, {PRODUCTS}) g""",
    f"""
    insert into {SRC}.orders
    select g,
           1 + (g * 7919) % {CUSTOMERS},
           (array['new', 'paid', 'shipped', 'cancelled'])[1 + g % 4]::order_status,
           timestamptz '2024-01-01' + (g * 17 || ' minutes')::interval,
           case when g % 3 = 0 then null
                else timestamptz '2024-01-01' + (g * 17 || ' minutes')::interval
                     + ((g % 96) || ' hours')::interval end,
           case when g % 5 = 0 then 'call before "delivery", floor ' || g % 20 else null end
    from generate_series(1, {ORDERS}) g""",
    f"""
    insert into {SRC}.order_items
    select row_number() over (),
           o.id,
           1 + (o.id * 31 + k * 17) % {PRODUCTS},
           1 + (o.id + k) % 5,
           p.price,
           ((o.id + k) % 4) * 0.05
    from {SRC}.orders o
    cross join generate_series(1, 1 + o.id % 4) k
    join {SRC}.products p on p.id = 1 + (o.id * 31 + k * 17) % {PRODUCTS}""",
    f"""
    insert into {SRC}.payments
    select row_number() over (),
           o.id,
           case when o.id % 4 = 0 then 10.00 else s.gross end,
           (array['card', 'cash', 'sbp'])[1 + o.id % 3],
           o.placed_at + interval '15 minutes',
           case when o.id % 2 = 0 then jsonb_build_object('auth', md5(o.id::text)) else null end
    from {SRC}.orders o
    join (select order_id, sum(qty * unit_price * (1 - discount))::numeric(14,2) as gross
          from {SRC}.order_items group by order_id) s on s.order_id = o.id
    where o.status in ('paid', 'shipped')""",
]

REPORT_SQL = f"""
with items as (
    select oi.order_id,
           count(*)                                              as lines,
           sum(oi.qty)                                           as units,
           sum(oi.qty * oi.unit_price * (1 - oi.discount))::numeric(14,2) as gross,
           array_agg(distinct p.category order by p.category)   as categories,
           jsonb_agg(jsonb_build_object('sku', p.sku, 'qty', oi.qty) order by oi.id)
                                                                 as lines_json
    from {SRC}.order_items oi
    join {SRC}.products p on p.id = oi.product_id
    group by oi.order_id
)
select o.id                                              as order_id,
       o.status,
       c.external_id                                     as customer_uid,
       c.name                                            as customer_name,
       lower(c.email)                                    as email,
       c.tier,
       c.address ->> 'city'                              as city,
       (c.address -> 'geo' ->> 'lat')::float8            as lat,
       c.tags,
       c.last_ip,
       o.placed_at,
       date_trunc('month', o.placed_at)::date            as month,
       o.shipped_at,
       o.shipped_at - o.placed_at                        as lead_time,
       coalesce(o.note, '')                              as note,
       i.lines,
       i.units,
       i.gross,
       i.categories,
       i.lines_json,
       pay.amount::numeric(14,2)                         as paid,
       pay.paid_at,
       pay.methods,
       case when pay.amount is null then 'unpaid'
            when pay.amount >= i.gross then 'paid'
            else 'partial' end                           as payment_state,
       (i.gross - coalesce(pay.amount, 0))::numeric(14,2) as balance,
       row_number() over (partition by c.id order by o.placed_at) as customer_order_no,
       sum(i.gross) over (partition by c.id)::numeric(16,2)      as customer_total,
       round(avg(i.gross) over (partition by date_trunc('month', o.placed_at)), 6)
           ::numeric(18,6)                               as month_avg
from {SRC}.orders o
join {SRC}.customers c on c.id = o.customer_id
left join items i on i.order_id = o.id
left join lateral (
    select sum(p.amount) as amount,
           max(p.paid_at) as paid_at,
           string_agg(distinct p.method, ',') as methods
    from {SRC}.payments p
    where p.order_id = o.id
) pay on true
where o.placed_at >= date '2024-01-01'
"""

EXPECTED_COLUMNS = [
    ("order_id", "bigint", True),
    ("status", "text", False),
    ("customer_uid", "uuid", False),
    ("customer_name", "text", False),
    ("email", "text", False),
    ("tier", "text", False),
    ("city", "text", False),
    ("lat", "double precision", False),
    ("tags", "text[]", False),
    ("last_ip", "inet", False),
    ("placed_at", "timestamp with time zone", False),
    ("month", "date", False),
    ("shipped_at", "timestamp with time zone", False),
    ("lead_time", "interval", False),
    ("note", "text", False),
    ("lines", "bigint", False),
    ("units", "bigint", False),
    ("gross", "numeric(14,2)", False),
    ("categories", "text[]", False),
    ("lines_json", "jsonb", False),
    ("paid", "numeric(14,2)", False),
    ("paid_at", "timestamp with time zone", False),
    ("methods", "text", False),
    ("payment_state", "text", False),
    ("balance", "numeric(14,2)", False),
    ("customer_order_no", "bigint", False),
    ("customer_total", "numeric(16,2)", False),
    ("month_avg", "numeric(18,6)", False),
]
"""Колонки таблицы приёмника (имя, тип, not null): enum источника ложатся
text по column_types, остальное — как описал стейтмент сервер источника."""

DECLARED = [ColumnDeclaration(name="order_id", nullable=False)]
"""Серверу nullable выборки неизвестен: not null у ключа объявляет LLM."""

RULES = ColumnRules(column_types={"status": "text", "tier": "text"})
"""enum источника на другом сервере нет — LLM объявляет text у приёмника."""


@pytest.fixture(scope="module")
async def source() -> AsyncIterator[PostgresSide]:
    side = PostgresSide(_newest_postgres(), SRC)
    await side.connect()
    await side.recreate_schema(SCHEMA_DDL)
    await side.execute(FILL_DML)
    await side.execute([f"create view {SRC}.orders_report as {REPORT_SQL}"])

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=_targets(), ids=lambda s: s.name)
async def target(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, DW)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


async def transfer(  # noqa: PLR0913
    source: PostgresSide,
    target: PostgresSide,
    table: str,
    schema: Any,
    delete: Any,
    mode: Engine,
    columns: Sequence[ColumnDeclaration] = DECLARED,
) -> str:
    """pg_sync_out на источнике -> pg_sync_in на приёмнике, два сервера."""
    pumps = Pumps(postgres=source.profile, postgres_target=target.profile)
    chained = await pumps.chain(
        Leg(
            "pg_sync_out",
            {
                "sql": REPORT_SQL,
                "target_engine": mode,
                "columns": columns,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
        Leg(
            "pg_sync_in",
            {
                "schema_name": DW,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": InsertFull(kind="full"),
                "rules": RULES,
                "copy_options": CopyOptions(
                    chunk_bytes=CHUNK, exact_floats=target.greenplum_6
                ),
            },
        ),
    )
    print(f"\n--- pg_sync_out ({mode.value}) ---\n{chained.out_report}")
    print(f"--- pg_sync_in ({mode.value}) ---\n{chained.in_report}")

    return chained.in_report


async def same_content(source: PostgresSide, target: PostgresSide, table: str) -> None:
    """Каждая колонка отчёта текстом совпадает с представлением источника."""
    report = Landing(source, SRC, "orders_report")
    landed = Landing(target, DW, table)
    for name, _, _ in EXPECTED_COLUMNS:
        expected = await report.texts(name, order_by="order_id")
        actual = await landed.texts(name, order_by="order_id")

        assert actual == expected, f"column {name} differs"


class TestOrdersReport:
    async def test_copy_mode_creates_and_fills_the_report(
        self, source: PostgresSide, target: PostgresSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            CreateIfNotExists(kind="create_if_not_exists"),
            DeleteNothing(kind="nothing"),
            Engine.POSTGRES,
        )
        landed = Landing(target, DW, "orders_report")

        assert f"{ORDERS} rows written" in report
        assert await landed.columns() == EXPECTED_COLUMNS
        assert await landed.count() == ORDERS
        assert await landed.aggregate("count(*) filter (where paid is null)") > 0
        assert await landed.aggregate("sum(balance)") == await Landing(
            source, SRC, "orders_report"
        ).aggregate("sum(balance)")
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, source: PostgresSide, target: PostgresSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
            Engine.POSTGRES,
        )

        assert "error" not in report.split("rows written")[0].lower()
        assert await Landing(target, DW, "orders_report").count() == ORDERS

    async def test_arrow_mode_lands_the_same_content(
        self, source: PostgresSide, target: PostgresSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report_arrow",
            CreateIfNotExists(kind="create_if_not_exists"),
            DeleteNothing(kind="nothing"),
            Engine.UNKNOWN,
        )
        landed = Landing(target, DW, "orders_report_arrow")

        assert f"{ORDERS} rows written" in report
        assert await landed.columns() == EXPECTED_COLUMNS
        assert await landed.count() == ORDERS
        assert await landed.aggregate("sum(gross)") == await Landing(
            source, SRC, "orders_report"
        ).aggregate("sum(gross)")
        assert isinstance(await landed.aggregate("max(month_avg)"), Decimal)
        await same_content(source, target, "orders_report_arrow")
