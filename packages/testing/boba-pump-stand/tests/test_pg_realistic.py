# ruff: noqa: S608, E501
"""Перелив отчёта по заказам между серверами postgres и Greenplum парой
postgres -> postgres (pg_stream_out и pg_stream_in) запросом, каким его написал
бы LLM: CTE, join и left join с NULL, lateral, оконные функции, агрегаты в
массив и jsonb, enum, uuid, inet, interval, date_trunc.

Источник — новейший postgres стенда, приёмники — pg-16 и Greenplum 7. Сценарии
идут по порядку на каждом приёмнике и опираются на таблицы предыдущих:
    - первая загрузка csv, повторная с проверкой схемы, tsv;
    - инкремент одного месяца через delete_where;
    - витрина приёмника со своими именами через rename_columns и column_types;
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table: distributed by у Greenplum, with (...) у postgres;
    - staging и подмена таблицы шагами after, откат при ошибке шага;
    - поток arrow между серверами;
    - сухой прогон do_nothing + insert nothing;
    - обратный путь: агрегат с приёмника на новейший postgres, отказ binary
      между разными мажорными версиями.
Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.
Подстановкой в описания идут имена схем стенда ($dw, $back) и текст
запроса отчёта $report_sql — он один на модуль и собран из имён стенда.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.pump_stand import Loaded, PostgresSide, PumpDags, PumpStand
from boba.stand.names import StandNames
from boba.toolrun.dag_run import DagOutcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
SRC = StandNames().of("shop")
DW = StandNames().of("shop_dw")
BACK = StandNames().of("shop_back")
CUSTOMERS = 12
PRODUCTS = 10
ORDERS = 24
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
           timestamptz '2024-01-01' + (g * 14160 || ' minutes')::interval,
           case when g % 3 = 0 then null
                else timestamptz '2024-01-01' + (g * 14160 || ' minutes')::interval
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

REPORT_CSV = """
name = "shop_report_csv"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""
"""order_id из join сервер считает nullable, LLM знает, что нет; enum
источника на приёмнике нет — status и tier ложатся text. Так у всех
описаний отчёта."""

REPORT_RELOAD = """
name = "shop_report_reload"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

REPORT_TSV = """
name = "shop_report_tsv"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "tsv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report_tsv"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

MONTH_REPLACED = """
name = "shop_month_replaced"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"select * from ($report_sql) r where r.month = date '2024-03-01'\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "month = date '2024-03-01'" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""
"""Окна посчитаны по всему отчёту, как в представлении источника."""

RENAMED_MART = """
name = "shop_renamed_mart"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"select order_id, customer_name, tier, paid, gross, placed_at from ($report_sql) r\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_mart"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { order_uid = "order_id", client = "customer_name", client_tier = "tier", paid_amount = "paid" }, column_types = { client_tier = "varchar(10)" } }
feed = "rows"
"""

DRIFT_CREATE = """
name = "shop_drift_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

DRIFT_REFUSED = """
name = "shop_drift_refused"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

DRIFT_BACKED_UP = """
name = "shop_drift_backed_up"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

TEMPLATE_POSTGRES = """
name = "shop_template_postgres"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_tpl"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
create_table = "create table {schema_name}.{table_name} ({columns}) with (fillfactor = 90)"
feed = "rows"
"""

TEMPLATE_GREENPLUM = """
name = "shop_template_greenplum"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_tpl"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
create_table = "create table {schema_name}.{table_name} ({columns}) distributed by (order_id)"
feed = "rows"
"""

SWAP_REFUSED = """
name = "shop_swap_refused"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
after = [
    "alter table $dw.orders_report rename to orders_report_old",
    "alter table $dw.orders_report_stage rename to orders_report",
    "do $$$$ begin raise exception 'swap refused'; end $$$$",
]
feed = "rows"
"""
"""Третий шаг after падает: откат всего, включая staging."""

SWAP = """
name = "shop_swap"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
after = [
    "alter table $dw.orders_report rename to orders_report_old",
    "alter table $dw.orders_report_stage rename to orders_report",
    "drop table $dw.orders_report_old",
]
feed = "rows"
"""
"""Загрузка в staging и подмена витрины шагами after одной транзакцией."""

REPORT_ARROW = """
name = "shop_report_arrow"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "arrow"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report_arrow"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

DRY_RUN = """
name = "shop_dry_run"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "shop"
sql = \"\"\"$report_sql\"\"\"
wire = "csv"
columns = [{ name = "order_id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "dw"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "nothing" }
rules = { column_types = { status = "text", tier = "text" } }
feed = "rows"
"""

CITY_MONTH = """
name = "shop_city_month"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "dw"
sql = "select city, month, count(*) as orders, sum(gross)::numeric(16,2) as gross, sum(balance)::numeric(16,2) as balance from $dw.orders_report group by city, month"
wire = "csv"
columns = []
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "back"
schema_name = "$back"
table_name = "city_month"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
feed = "rows"
"""

CITY_MONTH_BINARY = """
name = "shop_city_month_binary"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "dw"
sql = "select city, month, count(*) as orders, sum(gross)::numeric(16,2) as gross, sum(balance)::numeric(16,2) as balance from $dw.orders_report group by city, month"
wire = "binary"
columns = []
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "back"
schema_name = "$back"
table_name = "city_month_bin"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
feed = "rows"
"""


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


@pytest.fixture(scope="module")
async def back() -> AsyncIterator[PostgresSide]:
    """Схема на новейшем postgres, куда агрегат едет обратно с приёмника."""
    side = PostgresSide(_newest_postgres(), BACK)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


@pytest.fixture
def dags(tmp_path: Path, source: PostgresSide, target: PostgresSide) -> PumpDags:
    """Запуск описаний источник (shop) -> приёмник (dw), два сервера."""
    return PumpDags(
        tmp_path,
        {"shop": source.profile, "dw": target.profile},
        {"dw": DW, "report_sql": REPORT_SQL},
    )


@pytest.fixture
def back_dags(tmp_path: Path, target: PostgresSide, back: PostgresSide) -> PumpDags:
    """Запуск описаний обратного пути: приёмник (dw) -> новейший postgres."""
    return PumpDags(
        tmp_path,
        {"dw": target.profile, "back": back.profile},
        {"dw": DW, "back": BACK},
    )


async def transfer(dags: PumpDags, spec: str) -> DagOutcome:
    """Запуск описания; отчёты обоих насосов печатаются."""
    outcome = await dags.run(spec)
    print(f"\n--- {outcome.dag}: pg_stream_out ---\n{outcome.node('out').content}")
    print(f"--- pg_stream_in ---\n{outcome.node('in').content}")

    return outcome


def landed(outcome: DagOutcome) -> str:
    """Отчёт приёмника удавшегося запуска."""
    report = outcome.node("in").content

    assert outcome.ok(), report

    return report


def refused(outcome: DagOutcome, key: str, error_kind: str) -> str:
    """Текст отказа узла key, исходная ошибка которого — класса error_kind."""
    failure = outcome.failure(key)
    text = failure.llm_view()

    assert failure.error_kind == error_kind, text

    return text


async def same_content(source: PostgresSide, target: PostgresSide, table: str) -> None:
    """Каждая колонка отчёта текстом совпадает с представлением источника."""
    report = Loaded(source, SRC, "orders_report")
    landed_table = Loaded(target, DW, table)
    for name, _, _ in EXPECTED_COLUMNS:
        expected = await report.texts(name, order_by="order_id")
        actual = await landed_table.texts(name, order_by="order_id")

        assert actual == expected, f"column {name} differs"


class TestOrdersReport:
    async def test_csv_wire_creates_and_fills_the_report(
        self, dags: PumpDags, source: PostgresSide, target: PostgresSide
    ) -> None:
        report = landed(await transfer(dags, REPORT_CSV))
        landed_table = Loaded(target, DW, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed_table.columns() == EXPECTED_COLUMNS
        assert await landed_table.count() == ORDERS
        assert await landed_table.aggregate("count(*) filter (where paid is null)") > 0
        assert await landed_table.aggregate("sum(balance)") == await Loaded(
            source, SRC, "orders_report"
        ).aggregate("sum(balance)")
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, dags: PumpDags, target: PostgresSide
    ) -> None:
        report = landed(await transfer(dags, REPORT_RELOAD))

        assert "error" not in report.split("rows loaded")[0].lower()
        assert await Loaded(target, DW, "orders_report").count() == ORDERS

    async def test_tsv_wire_lands_the_same_content(
        self, dags: PumpDags, source: PostgresSide, target: PostgresSide
    ) -> None:
        report = landed(await transfer(dags, REPORT_TSV))
        landed_table = Loaded(target, DW, "orders_report_tsv")

        assert f"{ORDERS} rows loaded" in report
        assert await landed_table.columns() == EXPECTED_COLUMNS
        assert await landed_table.count() == ORDERS
        assert await landed_table.aggregate("sum(gross)") == await Loaded(
            source, SRC, "orders_report"
        ).aggregate("sum(gross)")
        assert isinstance(await landed_table.aggregate("max(month_avg)"), Decimal)
        await same_content(source, target, "orders_report_tsv")


class TestIncrementalMonth:
    """Перезаливка одного месяца в готовую витрину: delete_where по month
    удаляет только его, insert full кладёт свежие строки месяца."""

    MONTH: ClassVar[str] = "2024-03-01"

    async def test_one_month_is_replaced(
        self, dags: PumpDags, source: PostgresSide, target: PostgresSide
    ) -> None:
        landed_table = Loaded(target, DW, "orders_report")
        await target.execute(
            [
                f"update {DW}.orders_report set note = 'stale' "
                f"where month = date '{self.MONTH}'"
            ]
        )
        in_month = await Loaded(source, SRC, "orders_report").aggregate(
            f"count(*) filter (where month = date '{self.MONTH}')"
        )

        report = landed(await transfer(dags, MONTH_REPLACED))

        assert in_month > 0
        assert f"{in_month} rows deleted" in report
        assert f"{in_month} rows loaded" in report
        assert (
            await landed_table.aggregate("count(*) filter (where note = 'stale')") == 0
        )
        assert await landed_table.count() == ORDERS
        await same_content(source, target, "orders_report")


class TestRenamedMart:
    """Витрина приёмника со своими именами и типами: таблица создана заранее,
    error_if_not_exists не даёт её создать заново, rename_columns связывает
    колонки витрины с полями потока, column_types даёт enum-колонке тип
    витрины."""

    MART: ClassVar[str] = f"""
        create table {DW}.orders_mart (
            order_uid bigint not null,
            client text,
            client_tier varchar(10),
            paid_amount numeric(14,2),
            gross numeric(14,2),
            placed_at timestamptz
        )"""

    async def test_missing_mart_is_refused(self, dags: PumpDags) -> None:
        outcome = await transfer(dags, RENAMED_MART)

        assert "table is missing" in refused(outcome, "in", "TransferError")

    async def test_mart_is_filled_by_its_own_names(
        self, dags: PumpDags, source: PostgresSide, target: PostgresSide
    ) -> None:
        await target.execute([f"drop table if exists {DW}.orders_mart", self.MART])
        report_view = Loaded(source, SRC, "orders_report")
        mart = Loaded(target, DW, "orders_mart")

        report = landed(await transfer(dags, RENAMED_MART))

        assert f"{ORDERS} rows loaded" in report
        assert await mart.count() == ORDERS
        assert await mart.texts("client", order_by="order_uid") == (
            await report_view.texts("customer_name", order_by="order_id")
        )
        assert await mart.texts("client_tier", order_by="order_uid") == (
            await report_view.texts("tier", order_by="order_id")
        )
        assert await mart.aggregate("sum(paid_amount)") == (
            await report_view.aggregate("sum(paid)")
        )


class TestSchemaDrift:
    """Таблица на приёмнике разошлась с потоком: error_if_schema_changed
    отказывает с текстом расхождения, backup_and_create_if_schema_changed
    переименовывает её в _bak_<время> и создаёт заново."""

    async def test_drift_is_refused_then_backed_up(
        self, dags: PumpDags, target: PostgresSide
    ) -> None:
        drift = Loaded(target, DW, "orders_drift")
        landed(await transfer(dags, DRIFT_CREATE))
        await target.execute(
            [f"alter table {DW}.orders_drift alter column note type varchar(200)"]
        )

        outcome = await transfer(dags, DRIFT_REFUSED)

        assert "note: type differs" in refused(outcome, "in", "TransferError")

        report = landed(await transfer(dags, DRIFT_BACKED_UP))
        tables = await drift.tables()
        backups: list[str] = []
        for name in tables:
            if name.startswith("orders_drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "orders_drift_bak_" in report
        assert len(backups) == 1
        assert await Loaded(target, DW, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("note", "text", False) in await drift.columns()


class TestCreateTemplate:
    """Особенности таблицы из шаблона create table: у Greenplum ключ
    распределения, у postgres параметры хранения; drop_and_create дважды
    подряд пересоздаёт таблицу по тому же шаблону."""

    async def test_table_gets_its_storage_options(
        self, dags: PumpDags, target: PostgresSide
    ) -> None:
        spec = TEMPLATE_POSTGRES
        if target.greenplum:
            spec = TEMPLATE_GREENPLUM

        for _ in range(2):
            report = landed(await transfer(dags, spec))

            assert f"{ORDERS} rows loaded" in report

        landed_table = Loaded(target, DW, "orders_tpl")
        if target.greenplum:
            policy = await landed_table.aggregate(
                "(select distkey::text from gp_distribution_policy "
                f"where localoid = '{DW}.orders_tpl'::regclass)"
            )

            assert policy == "1"
        else:
            options = await landed_table.aggregate(
                "(select reloptions::text from pg_class "
                f"where oid = '{DW}.orders_tpl'::regclass)"
            )

            assert options == "{fillfactor=90}"

        assert await landed_table.count() == ORDERS


class TestStagingSwap:
    """Загрузка в staging и подмена витрины шагами after одной транзакцией:
    ошибка шага откатывает всё, включая staging; успешная подмена оставляет
    одну витрину со свежими строками."""

    async def test_failed_swap_leaves_the_mart_intact(
        self, dags: PumpDags, target: PostgresSide
    ) -> None:
        landed_table = Loaded(target, DW, "orders_report")

        outcome = await transfer(dags, SWAP_REFUSED)

        assert "swap refused" in refused(outcome, "in", "RaiseException")

        tables = await landed_table.tables()

        assert "orders_report" in tables
        assert "orders_report_stage" not in tables
        assert "orders_report_old" not in tables
        assert await landed_table.count() == ORDERS

    async def test_swap_replaces_the_mart(
        self, dags: PumpDags, source: PostgresSide, target: PostgresSide
    ) -> None:
        landed_table = Loaded(target, DW, "orders_report")
        await target.execute([f"update {DW}.orders_report set note = 'old'"])

        report = landed(await transfer(dags, SWAP))
        tables = await landed_table.tables()

        assert f"{ORDERS} rows loaded" in report
        assert "orders_report_stage" not in tables
        assert "orders_report_old" not in tables
        assert await landed_table.aggregate("count(*) filter (where note = 'old')") == 0
        await same_content(source, target, "orders_report")


class TestArrowBetweenServers:
    """Тот же отчёт потоком arrow: нейтральный контракт, enum через
    column_types; содержимое совпадает с представлением источника."""

    async def test_arrow_lands_the_report(
        self, dags: PumpDags, source: PostgresSide, target: PostgresSide
    ) -> None:
        report = landed(await transfer(dags, REPORT_ARROW))
        landed_table = Loaded(target, DW, "orders_report_arrow")
        view = Loaded(source, SRC, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed_table.count() == ORDERS
        assert await landed_table.aggregate("sum(gross)") == await view.aggregate(
            "sum(gross)"
        )
        assert await landed_table.aggregate("sum(balance)") == await view.aggregate(
            "sum(balance)"
        )
        for name in ("order_id", "customer_uid", "customer_name", "city", "tier"):
            assert await landed_table.texts(name, order_by="order_id") == (
                await view.texts(name, order_by="order_id")
            ), f"column {name} differs"


class TestDryRun:
    """do_nothing и insert nothing: поток прочитан до конца, таблица не
    тронута ни схемой, ни данными."""

    async def test_nothing_changes(self, dags: PumpDags, target: PostgresSide) -> None:
        landed_table = Loaded(target, DW, "orders_report")
        columns = await landed_table.columns()

        report = landed(await transfer(dags, DRY_RUN))

        assert report.startswith("0 rows loaded")
        assert await landed_table.columns() == columns
        assert await landed_table.count() == ORDERS


class TestBackToSource:
    """Обратный путь: агрегат «город × месяц» с приёмника на новейший
    postgres; суммы совпадают с тем же агрегатом по представлению источника.
    binary между разными мажорными версиями приёмник отвергает."""

    AGGREGATE: ClassVar[str] = (
        f"select city, month, count(*) as orders, "
        f"sum(gross)::numeric(16,2) as gross, "
        f"sum(balance)::numeric(16,2) as balance "
        f"from {DW}.orders_report group by city, month"
    )
    """Тот же агрегат, что в описании CITY_MONTH: по нему строится
    представление источника для сверки."""
    ROW: ClassVar[str] = "city || ' ' || month || ' ' || orders || ' ' || gross"

    async def test_city_month_comes_back(
        self, back_dags: PumpDags, source: PostgresSide, back: PostgresSide
    ) -> None:
        report = landed(await transfer(back_dags, CITY_MONTH))
        city_month = Loaded(back, BACK, "city_month")
        view = Loaded(source, SRC, "orders_report")
        expected = await view.aggregate("count(distinct (city, month))")

        assert f"{expected} rows loaded" in report
        assert await city_month.aggregate("sum(orders)") == ORDERS
        assert await city_month.aggregate("sum(gross)") == await view.aggregate(
            "sum(gross)"
        )
        assert await city_month.aggregate("sum(balance)") == await view.aggregate(
            "sum(balance)"
        )
        await source.execute(
            [
                f"create or replace view {SRC}.city_month as "
                f"{self.AGGREGATE.replace(DW + '.', SRC + '.')}"
            ]
        )
        landed_rows = sorted(await city_month.scalars(self.ROW, order_by="city"))
        source_rows = sorted(
            await Loaded(source, SRC, "city_month").scalars(self.ROW, order_by="city")
        )

        assert landed_rows == source_rows

    async def test_binary_across_major_versions_is_refused(
        self, back_dags: PumpDags, target: PostgresSide, back: PostgresSide
    ) -> None:
        assert target.version // 10000 != back.version // 10000, (
            f"{target.source.name} and {back.source.name} share the major version: "
            "the case needs targets older than the newest postgres"
        )

        outcome = await transfer(back_dags, CITY_MONTH_BINARY)

        assert "major versions differ" in refused(outcome, "in", "TransferError")
