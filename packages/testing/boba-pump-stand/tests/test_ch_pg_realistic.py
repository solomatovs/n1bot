# ruff: noqa: S608, E501
"""Перелив отчётов магазина из ClickHouse в postgres и Greenplum парой
ClickHouse -> postgres (ch_stream_out и pg_stream_in) запросами, какими их
написал бы LLM: CTE, join и left join с join_use_nulls, оконные функции,
агрегаты в массив и JSON, Enum, LowCardinality, UUID, IPv4, Decimal,
DateTime64 в UTC и в поясе, UInt64 из count().

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель; тело
отчёта и запросы трафика подставляются в описание именами $report_body,
$traffic_sql и $traffic_json_sql, JSON-тип приёмника — $json_type.

Источник — каждый ClickHouse стенда, приёмник — каждый postgres (9.0–19) и
Greenplum (6, 7) стенда. JSON-колонки LLM объявляет тем типом, что есть у
приёмника: jsonb с 9.4, json с 9.2, раньше text.
Сценарии идут по порядку на каждой паре и опираются на таблицы предыдущих:
    - первая загрузка отчёта по заказам, повторная с проверкой схемы;
    - инкремент одного месяца через delete_where;
    - витрина приёмника со своими именами через rename_columns и column_types;
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table: distributed by у Greenplum, with (...) у postgres;
    - staging и подмена таблицы шагами after, откат при ошибке шага;
    - сухой прогон do_nothing + insert nothing;
    - трафик по дням: массив и время в поясе Москвы сначала отвергнуты,
      затем varchar по fallback_as_varchar, затем запрос переписан с
      toJSONString и column_types;
    - поток arrow того же отчёта;
    - обратный путь: агрегат «город × месяц» с приёмника в ClickHouse по tsv.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Loaded,
    PostgresSide,
    PumpDags,
    PumpStand,
)
from boba.stand.names import StandNames

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
SRC = StandNames().of("shop_ch")
DW = StandNames().of("shop_ch_dw")
CUSTOMERS = 12
PRODUCTS = 10
ORDERS = 24
VIEWS = 24
MONTH = "2024-03-01"
JOIN_NULLS = "settings join_use_nulls = 1"


SCHEMA_DDL = [
    f"""
    create table {SRC}.customers (
        id UInt64,
        external_id UUID,
        name String,
        email String,
        tier Enum8('bronze' = 1, 'silver' = 2, 'gold' = 3),
        signed_up DateTime('UTC'),
        city LowCardinality(String),
        tags Array(String),
        last_ip Nullable(IPv4),
        is_active Bool
    ) engine = MergeTree order by id""",
    f"""
    create table {SRC}.products (
        id UInt32,
        sku FixedString(10),
        title String,
        category LowCardinality(String),
        price Decimal(12, 2),
        weight_kg Nullable(Float32),
        attrs Map(String, String)
    ) engine = MergeTree order by id""",
    f"""
    create table {SRC}.orders (
        id UInt64,
        customer_id UInt64,
        status Enum8('new' = 1, 'paid' = 2, 'shipped' = 3, 'cancelled' = 4),
        placed_at DateTime64(3, 'UTC'),
        shipped_at Nullable(DateTime64(3, 'UTC')),
        note Nullable(String)
    ) engine = MergeTree order by id""",
    f"""
    create table {SRC}.order_items (
        order_id UInt64,
        line UInt8,
        product_id UInt32,
        qty UInt16,
        unit_price Decimal(12, 2),
        discount Decimal(5, 4)
    ) engine = MergeTree order by (order_id, line)""",
    f"""
    create table {SRC}.payments (
        order_id UInt64,
        amount Decimal(14, 2),
        method LowCardinality(String),
        paid_at DateTime64(3, 'UTC')
    ) engine = MergeTree order by order_id""",
    f"""
    create table {SRC}.page_views (
        ts DateTime64(6, 'UTC'),
        session UUID,
        customer_id Nullable(UInt64),
        url LowCardinality(String),
        referrer Nullable(String),
        ip IPv6,
        duration_ms UInt32,
        device Enum8('desktop' = 1, 'mobile' = 2, 'tablet' = 3),
        utm Map(String, String)
    ) engine = MergeTree order by ts""",
]

FILL_DML = [
    f"""
    insert into {SRC}.customers
    select number,
           generateUUIDv4(),
           concat('Customer ', toString(number)),
           concat('user', toString(number), '@Example.COM'),
           ['bronze', 'silver', 'gold'][1 + number % 3],
           toDateTime('2023-01-01 00:00:00', 'UTC') + number * 3600,
           ['Moscow', 'Kazan', 'Perm'][1 + number % 3],
           arraySlice([concat('t', toString(number % 5)), 'vip'], 1, 1 + number % 2),
           if(number % 7 = 0, null,
              toIPv4(concat('10.0.', toString(number % 256), '.', toString(number % 250)))),
           number % 11 != 0
    from numbers(1, {CUSTOMERS})""",
    f"""
    insert into {SRC}.products
    select number,
           concat('SKU-', leftPad(toString(number), 6, '0')),
           concat('Product ', toString(number)),
           ['books', 'toys', 'food', 'tools'][1 + number % 4],
           toDecimal64(1 + cityHash64(number) % 50000, 2) / 100,
           if(number % 9 = 0, null, (cityHash64(number) % 2000) / 100),
           map('color', ['red', 'blue'][1 + number % 2], 'stock', toString(number % 100))
    from numbers(1, {PRODUCTS})""",
    f"""
    insert into {SRC}.orders
    select number,
           1 + (number * 7919) % {CUSTOMERS},
           ['new', 'paid', 'shipped', 'cancelled'][1 + number % 4],
           fromUnixTimestamp64Milli(toInt64(1704067200000 + number * 849600000), 'UTC'),
           if(number % 3 = 0, null,
              fromUnixTimestamp64Milli(
                  toInt64(1704067200000 + number * 849600000 + (number % 96) * 3600000), 'UTC')),
           if(number % 5 = 0,
              concat('call before "delivery", floor ', toString(number % 20), '\\t\\\\ back'),
              null)
    from numbers(1, {ORDERS})""",
    f"""
    insert into {SRC}.order_items
    select o.id, o.k, o.pid, 1 + (o.id + o.k) % 5, p.price,
           toDecimal32((o.id + o.k) % 4 * 5, 4) / 100
    from (
        select id, k, toUInt32(1 + (id * 31 + k * 17) % {PRODUCTS}) as pid
        from (select id, arrayJoin(range(1, 2 + id % 4)) as k from {SRC}.orders)
    ) o
    inner join {SRC}.products p on p.id = o.pid""",
    f"""
    insert into {SRC}.payments
    select o.id, if(o.id % 8 = 2, toDecimal64(10, 2), s.gross),
           ['card', 'cash', 'sbp'][1 + o.id % 3], o.placed_at + toIntervalMinute(15)
    from {SRC}.orders o
    inner join (
        select order_id,
               toDecimal64(sum(qty * unit_price * (1 - discount)), 2) as gross
        from {SRC}.order_items
        group by order_id
    ) s on s.order_id = o.id
    where o.status in ('paid', 'shipped')""",
    f"""
    insert into {SRC}.payments
    select id, toDecimal64(1, 2), 'bonus', placed_at + toIntervalHour(1)
    from {SRC}.orders
    where status = 'paid' and id % 6 = 1""",
    f"""
    insert into {SRC}.page_views
    select fromUnixTimestamp64Micro(toInt64(1704067200000000 + number * 252000000000), 'UTC'),
           toUUID(concat('00000000-0000-4000-8000-',
                         leftPad(toString(intDiv(number, 7)), 12, '0'))),
           if(number % 4 = 0, null, 1 + number % {CUSTOMERS}),
           concat('/p/', toString(number % 40)),
           if(number % 3 = 0, null, ['google', 'yandex', 'direct'][1 + number % 3]),
           if(number % 2 = 0,
              toIPv6(concat('2001:db8::', lower(hex(toUInt16(number % 65536))))),
              IPv4ToIPv6(toIPv4(concat('10.1.', toString(number % 256), '.1')))),
           toUInt32(cityHash64(number) % 60000),
           ['desktop', 'mobile', 'tablet'][1 + number % 3],
           map('source', ['ads', 'mail', 'social'][1 + number % 3],
               'campaign', concat('c', toString(number % 5)))
    from numbers(1, {VIEWS})""",
]

REPORT_BODY = f"""
with items as (
    select oi.order_id as order_id,
           count() as lines,
           sum(oi.qty) as units,
           toDecimal64(sum(oi.qty * oi.unit_price * (1 - oi.discount)), 2) as gross,
           toJSONString(arraySort(groupUniqArray(toString(p.category)))) as categories,
           toJSONString(arrayMap(t -> map('sku', t.2, 'qty', toString(t.3)),
                        arraySort(groupArray((oi.line, toString(p.sku), oi.qty)))))
               as lines_json
    from {SRC}.order_items oi
    inner join {SRC}.products p on p.id = oi.product_id
    group by oi.order_id
),
pay as (
    select order_id,
           sum(amount) as paid_sum,
           max(paid_at) as last_paid_at,
           arrayStringConcat(arraySort(groupUniqArray(toString(method))), ',') as methods
    from {SRC}.payments
    group by order_id
)
select o.id as order_id,
       o.status as status,
       c.external_id as customer_uid,
       c.name as customer_name,
       lower(c.email) as email,
       c.tier as tier,
       c.city as city,
       toJSONString(c.tags) as tags,
       c.last_ip as last_ip,
       c.is_active as is_active,
       c.signed_up as signed_up,
       o.placed_at as placed_at,
       toStartOfMonth(o.placed_at) as month,
       o.shipped_at as shipped_at,
       toInt64(dateDiff('second', o.placed_at, o.shipped_at)) as lead_time_s,
       ifNull(o.note, '') as note,
       i.lines as lines,
       i.units as units,
       i.gross as gross,
       i.categories as categories,
       i.lines_json as lines_json,
       pay.paid_sum as paid,
       pay.last_paid_at as paid_at,
       pay.methods as methods,
       multiIf(pay.paid_sum is null, 'unpaid',
               pay.paid_sum >= i.gross, 'paid',
               'partial') as payment_state,
       toDecimal64(i.gross - ifNull(pay.paid_sum, 0), 2) as balance,
       row_number() over (partition by o.customer_id order by o.placed_at)
           as customer_order_no,
       sum(i.gross) over (partition by o.customer_id) as customer_total,
       avg(toFloat64(i.gross)) over (partition by toStartOfMonth(o.placed_at))
           as month_avg
from {SRC}.orders o
inner join {SRC}.customers c on c.id = o.customer_id
inner join items i on i.order_id = o.id
left join pay on pay.order_id = o.id
where o.placed_at >= toDateTime64('2024-01-01 00:00:00', 3, 'UTC')
"""

EXPECTED_COLUMNS = [
    ("order_id", "numeric(20,0)", True),
    ("status", "text", True),
    ("customer_uid", "uuid", True),
    ("customer_name", "text", True),
    ("email", "text", True),
    ("tier", "text", True),
    ("city", "text", True),
    ("tags", "{json}", True),
    ("last_ip", "inet", False),
    ("is_active", "boolean", True),
    ("signed_up", "timestamp(0) with time zone", True),
    ("placed_at", "timestamp(3) with time zone", True),
    ("month", "date", True),
    ("shipped_at", "timestamp(3) with time zone", False),
    ("lead_time_s", "bigint", False),
    ("note", "text", True),
    ("lines", "numeric(20,0)", True),
    ("units", "numeric(20,0)", True),
    ("gross", "numeric(18,2)", True),
    ("categories", "{json}", True),
    ("lines_json", "{json}", True),
    ("paid", "numeric(38,2)", False),
    ("paid_at", "timestamp(3) with time zone", False),
    ("methods", "text", False),
    ("payment_state", "text", True),
    ("balance", "numeric(18,2)", True),
    ("customer_order_no", "numeric(20,0)", True),
    ("customer_total", "numeric(38,2)", True),
    ("month_avg", "double precision", True),
]
"""Колонки таблицы приёмника (имя, тип, not null): JSON-строки ложатся типом
json_type(target) по column_types, остальное — перевод типов ClickHouse; Nullable — только у
левого join и Nullable-колонок источника."""

COMPARED: Mapping[str, tuple[str, str]] = {
    "order_id": ("{c}", "{c}"),
    "last_ip": ("host({c})", "toString({c})"),
    "is_active": ("{c}", "{c}"),
    "signed_up": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS')",
        "toString({c})",
    ),
    "placed_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')",
        "toString({c})",
    ),
    "shipped_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')",
        "toString({c})",
    ),
    "paid_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')",
        "toString({c})",
    ),
    "gross": ("{c}", "{c}"),
    "paid": ("{c}", "{c}"),
    "balance": ("{c}", "{c}"),
    "customer_total": ("{c}", "{c}"),
    "month_avg": ("{c}", "{c}"),
}
"""Как сравнить колонку отчёта: выражение postgres и выражение ClickHouse
с одинаковым значением. Числа сравниваются значением (ClickHouse печатает
Decimal без хвостовых нулей; order_id::text в postgres дал бы сортировку по
тексту), время — текстом в UTC, inet — адресом без маски; остальные —
::text против toString."""

JSON_COLUMNS = ("tags", "categories", "lines_json")
FLOAT_COLUMNS = ("month_avg",)

TRAFFIC_BODY = f"""
select toDate(ts) as day,
       device,
       count() as views,
       uniqExact(session) as sessions,
       uniqExact(customer_id) as customers,
       countIf(customer_id is null) as anonymous,
       quantileExact(0.5)(duration_ms) as p50_ms,
       avg(duration_ms) as avg_ms,
       {{top_urls}} as top_urls,
       max(toDateTime64(ts, 3, 'Europe/Moscow')) as last_seen_msk
from {SRC}.page_views
group by day, device
"""
TOP_URLS = "arraySlice(arraySort(groupUniqArray(toString(url))), 1, 3)"
TRAFFIC_SQL = TRAFFIC_BODY.format(top_urls=TOP_URLS)
TRAFFIC_JSON_SQL = TRAFFIC_BODY.format(top_urls=f"toJSONString({TOP_URLS})")

REPORT_CREATE = """
name = "ch_to_pg_report_create"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

REPORT_RELOAD = """
name = "ch_to_pg_report_reload"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

REPORT_MONTH = """
name = "ch_to_pg_report_month"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''select * from ($report_body) r where r.month = toDate('$month') settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "month = date '$month'" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

MART = """
name = "ch_to_pg_report_mart"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''select order_id, customer_name, tier, paid, gross, placed_at from ($report_body) r settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_mart"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { order_uid = "order_id", client = "customer_name", client_tier = "tier", paid_amount = "paid" }, column_types = { order_uid = "bigint", client_tier = "varchar(10)" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

DRIFT_CREATE = """
name = "ch_to_pg_report_drift_create"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

DRIFT_CHECK = """
name = "ch_to_pg_report_drift_check"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

DRIFT_BACKUP = """
name = "ch_to_pg_report_drift_backup"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

TEMPLATE_GREENPLUM = """
name = "ch_to_pg_report_template_greenplum"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_tpl"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) distributed by (order_id)"
feed = "rows"
"""

TEMPLATE_POSTGRES = """
name = "ch_to_pg_report_template_postgres"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_tpl"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) with (fillfactor = 90)"
feed = "rows"
"""

# $$ в шагах after удвоен: string.Template сворачивает $$ в $
SWAP_REFUSED = """
name = "ch_to_pg_report_swap_refused"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
after = [
    "alter table $dw.orders_report rename to orders_report_old",
    "alter table $dw.orders_report_stage rename to orders_report",
    "do $$$$ begin raise exception 'swap refused'; end $$$$",
]
feed = "rows"
"""

SWAP = """
name = "ch_to_pg_report_swap"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
after = [
    "alter table $dw.orders_report rename to orders_report_old",
    "alter table $dw.orders_report_stage rename to orders_report",
    "drop table $dw.orders_report_old",
]
feed = "rows"
"""

DRY_RUN = """
name = "ch_to_pg_report_dry_run"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$report_body settings join_use_nulls = 1'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "nothing" }
rules = { column_types = { tags = "$json_type", categories = "$json_type", lines_json = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

TRAFFIC_ARRAY_REFUSED = """
name = "ch_to_pg_traffic_array_refused"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$traffic_sql'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "traffic"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

TRAFFIC_LOCAL_TIME_REFUSED = """
name = "ch_to_pg_traffic_local_time_refused"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$traffic_json_sql'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "traffic"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { top_urls = "$json_type" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

TRAFFIC_FALLBACK = """
name = "ch_to_pg_traffic_fallback"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$traffic_sql'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "traffic"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
feed = "rows"
"""

TRAFFIC_REWRITTEN = """
name = "ch_to_pg_traffic_rewritten"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''$traffic_json_sql'''
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "traffic"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { top_urls = "$json_type", last_seen_msk = "timestamp(3)" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

REPORT_ARROW = """
name = "ch_to_pg_report_arrow"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''select order_id, toString(status) as status, toString(customer_uid) as customer_uid, customer_name, toString(city) as city, gross, paid, placed_at from ($report_body) r settings join_use_nulls = 1, output_format_arrow_string_as_string = 1'''
wire = "arrow"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$dw"
table_name = "orders_report_arrow"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { customer_uid = "uuid" } }
unknown_types = { kind = "fail_on_unknown" }
feed = "rows"
"""

CITY_MONTH = """
name = "pg_to_ch_city_month"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select city, month, count(*) as orders, sum(gross)::numeric(18,2) as gross, sum(balance)::numeric(18,2) as balance from $dw.orders_report group by city, month"
wire = "tsv"
columns = [{ name = "city", nullable = false }, { name = "month", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$src"
table_name = "city_month"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "(city, month)"
feed = "rows"
"""


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def source(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, SRC)
    await side.connect()
    await side.recreate_database()
    for statement in [*SCHEMA_DDL, *FILL_DML]:
        await side.command(statement)

    await side.command(
        f"create table {SRC}.orders_report engine = MergeTree order by order_id "
        f"as {REPORT_BODY}",
        settings={"join_use_nulls": 1},
    )
    await side.command(
        f"create table {SRC}.traffic engine = MergeTree order by (day, device) "
        f"as {TRAFFIC_SQL}"
    )

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def target(request: Any, source: ClickHouseSide) -> AsyncIterator[PostgresSide]:
    """Схема приёмника пересоздаётся на каждый ClickHouse: сценарии
    опираются на таблицы предыдущих."""
    side = PostgresSide(request.param, DW)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


@pytest.fixture
def dags(tmp_path: Path, source: ClickHouseSide, target: PostgresSide) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"ch": source.profile, "pg": target.profile},
        {
            "src": SRC,
            "dw": DW,
            "month": MONTH,
            "json_type": json_type(target),
            "report_body": REPORT_BODY,
            "traffic_sql": TRAFFIC_SQL,
            "traffic_json_sql": TRAFFIC_JSON_SQL,
        },
    )


JSONB_SINCE = 90400
JSON_SINCE = 90200


def json_type(target: PostgresSide) -> str:
    """Тип, которым LLM объявит JSON-строку у приёмника этой версии."""
    if target.version >= JSONB_SINCE:
        return "jsonb"

    if target.version >= JSON_SINCE:
        return "json"

    return "text"


def expected_columns(target: PostgresSide) -> list[tuple[str, str, bool]]:
    json_kind = json_type(target)
    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_COLUMNS:
        columns.append((name, kind.format(json=json_kind), not_null))

    return columns


async def _landed(
    dags: PumpDags, source: ClickHouseSide, target: PostgresSide, spec: str
) -> str:
    """Запуск описания; отчёт приёмника — узла pg, отчёт источника — узла src."""
    outcome = await dags.run(spec)
    report = outcome.node("pg").content
    print(
        f"\n--- {source.source.name} -> {target.source.name}: {outcome.dag} ---\n"
        f"{outcome.node('src').content}"
    )
    print(f"--- pg_stream_in ---\n{report}")

    assert outcome.ok(), report

    return report


def parsed_json(texts: Sequence[Any]) -> list[Any]:
    """JSON-колонка значениями: jsonb postgres печатает с пробелами и в своём
    порядке ключей, toJSONString — без пробелов."""
    parsed: list[Any] = []
    for text in texts:
        parsed.append(json.loads(text))

    return parsed


async def same_content(
    source: ClickHouseSide, target: PostgresSide, table: str
) -> None:
    """Каждая колонка отчёта на приёмнике совпадает с отчётом ClickHouse."""
    report = ChLoaded(source, "orders_report")
    landed = Loaded(target, DW, table)
    for name, _, _ in expected_columns(target):
        pg_template, ch_template = COMPARED.get(name, ("{c}::text", "toString({c})"))
        actual = await landed.scalars(pg_template.format(c=name), order_by="order_id")
        expected = await report.column(ch_template.format(c=name), order_by="order_id")
        if name in JSON_COLUMNS:
            assert parsed_json(actual) == parsed_json(expected), (
                f"column {name} differs"
            )

            continue

        if name in FLOAT_COLUMNS:
            for got, want in zip(actual, expected, strict=True):
                assert abs(got - want) <= abs(want) * 1e-12, f"column {name} differs"

            continue

        assert actual == expected, f"column {name} differs"


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT_CREATE)
        landed = Loaded(target, DW, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed.columns() == expected_columns(target)
        assert await landed.count() == ORDERS
        assert await landed.aggregate("count(case when paid is null then 1 end)") > 0
        assert (
            await landed.aggregate("count(case when note like '%\t%' then 1 end)") > 0
        )
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT_RELOAD)

        assert "error" not in report.split("rows loaded")[0].lower()
        assert await Loaded(target, DW, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    """Перезаливка одного месяца в готовую витрину: delete_where по month
    удаляет только его, insert full кладёт свежие строки месяца."""

    async def test_one_month_is_replaced(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")
        await target.execute(
            [
                f"update {DW}.orders_report set note = 'stale' "
                f"where month = date '{MONTH}'"
            ]
        )
        in_month = await ChLoaded(source, "orders_report").scalar(
            f"countIf(month = toDate('{MONTH}'))"
        )

        report = await _landed(dags, source, target, REPORT_MONTH)

        assert in_month > 0
        assert f"{in_month} rows deleted" in report
        assert f"{in_month} rows loaded" in report
        assert await landed.aggregate("count(case when note = 'stale' then 1 end)") == 0
        assert await landed.count() == ORDERS
        await same_content(source, target, "orders_report")


class TestRenamedMart:
    """Витрина приёмника со своими именами и типами: rename_columns связывает
    колонки витрины с полями потока, column_types сужает UInt64 до bigint и
    Enum до varchar(10); timestamptz витрины точнее потока — предупреждение."""

    MART_DDL = f"""
        create table {DW}.orders_mart (
            order_uid bigint not null,
            client text not null,
            client_tier varchar(10) not null,
            paid_amount numeric(38,2),
            gross numeric(18,2) not null,
            placed_at timestamptz not null
        )"""

    async def test_missing_mart_is_refused(self, dags: PumpDags) -> None:
        outcome = await dags.run(MART)

        failure = outcome.failure("pg")
        assert failure.error_kind == "TransferError", failure
        assert "table is missing" in failure.llm_view()

    async def test_mart_is_filled_by_its_own_names(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        await target.execute([f"drop table if exists {DW}.orders_mart", self.MART_DDL])
        view = ChLoaded(source, "orders_report")
        mart = Loaded(target, DW, "orders_mart")

        report = await _landed(dags, source, target, MART)

        assert f"{ORDERS} rows loaded" in report
        assert re.search(
            r'"column": "placed_at",[^}]*"note": "table [^"]+ is finer than', report
        )
        assert await mart.count() == ORDERS
        assert await mart.texts("client", order_by="order_uid") == (
            await view.column("customer_name", order_by="order_id")
        )
        assert await mart.texts("client_tier", order_by="order_uid") == (
            await view.column("toString(tier)", order_by="order_id")
        )
        assert await mart.aggregate("sum(paid_amount)") == await view.scalar(
            "sum(paid)"
        )


class TestSchemaDrift:
    """Таблица на приёмнике разошлась с потоком: error_if_schema_changed
    отказывает с текстом расхождения, backup_and_create_if_schema_changed
    переименовывает её в _bak_<время> и создаёт заново."""

    async def test_drift_is_refused_then_backed_up(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        drift = Loaded(target, DW, "orders_drift")
        await _landed(dags, source, target, DRIFT_CREATE)
        await target.execute(
            [f"alter table {DW}.orders_drift alter column note type varchar(200)"]
        )

        refused = await dags.run(DRIFT_CHECK)

        failure = refused.failure("pg")
        assert failure.error_kind == "TransferError", failure
        assert "note: type differs" in failure.llm_view()

        report = await _landed(dags, source, target, DRIFT_BACKUP)
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("orders_drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "orders_drift_bak_" in report
        assert len(backups) == 1
        assert await Loaded(target, DW, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("note", "text", True) in await drift.columns()


class TestCreateTemplate:
    """Особенности таблицы из шаблона create table: у Greenplum ключ
    распределения, у postgres параметры хранения."""

    async def test_table_gets_its_storage_options(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        spec = TEMPLATE_POSTGRES
        if target.greenplum:
            spec = TEMPLATE_GREENPLUM

        for _ in range(2):
            report = await _landed(dags, source, target, spec)

            assert f"{ORDERS} rows loaded" in report

        landed = Loaded(target, DW, "orders_tpl")
        if target.greenplum:
            policy = await landed.aggregate(
                "(select distkey::text from gp_distribution_policy "
                f"where localoid = '{DW}.orders_tpl'::regclass)"
            )

            assert policy == "1"
        else:
            options = await landed.aggregate(
                "(select reloptions::text from pg_class "
                f"where oid = '{DW}.orders_tpl'::regclass)"
            )

            assert options == "{fillfactor=90}"

        assert await landed.count() == ORDERS


class TestStagingSwap:
    """Загрузка в staging и подмена витрины шагами after одной транзакцией:
    ошибка шага откатывает всё, включая staging."""

    async def test_failed_swap_leaves_the_mart_intact(
        self, dags: PumpDags, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")

        outcome = await dags.run(SWAP_REFUSED)

        failure = outcome.failure("pg")
        assert "swap refused" in failure.llm_view()

        tables = await landed.tables()

        assert "orders_report" in tables
        assert "orders_report_stage" not in tables
        assert "orders_report_old" not in tables
        assert await landed.count() == ORDERS

    async def test_swap_replaces_the_mart(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")
        await target.execute([f"update {DW}.orders_report set note = 'old'"])

        report = await _landed(dags, source, target, SWAP)
        tables = await landed.tables()

        assert f"{ORDERS} rows loaded" in report
        assert "orders_report_stage" not in tables
        assert await landed.aggregate("count(case when note = 'old' then 1 end)") == 0
        await same_content(source, target, "orders_report")


class TestDryRun:
    """do_nothing и insert nothing: поток прочитан до конца, таблица не
    тронута ни схемой, ни данными."""

    async def test_nothing_changes(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")
        columns = await landed.columns()

        report = await _landed(dags, source, target, DRY_RUN)

        assert report.startswith("0 rows loaded")
        assert await landed.columns() == columns
        assert await landed.count() == ORDERS


class TestDailyTraffic:
    """Трафик по дням и устройствам, как LLM пишет его с первой попытки:
    массив адресов и время в поясе Москвы у postgres пары не имеют. Отказ
    называет первую такую колонку, LLM правит по одной; fallback_as_varchar
    кладёт их текстом ClickHouse;
    переписанный запрос (toJSONString) с column_types даёт JSON-тип приёмника и
    timestamp(3) с временем Москвы."""

    ROW = (
        "day::text || ' ' || device || ' ' || views || ' ' || sessions || ' ' "
        "|| customers || ' ' || anonymous || ' ' || p50_ms",
        "concat(toString(day), ' ', toString(device), ' ', toString(views), ' ', "
        "toString(sessions), ' ', toString(customers), ' ', toString(anonymous), "
        "' ', toString(p50_ms))",
    )

    async def rows_match(
        self, source: ClickHouseSide, target: PostgresSide, pg: str, ch: str
    ) -> None:
        landed = sorted(await Loaded(target, DW, "traffic").scalars(pg, order_by="day"))
        expected = sorted(await ChLoaded(source, "traffic").column(ch, order_by="day"))

        assert landed == expected

    async def test_array_is_refused_first(self, dags: PumpDags) -> None:
        outcome = await dags.run(TRAFFIC_ARRAY_REFUSED)

        failure = outcome.failure("pg")
        assert failure.error_kind == "TransferError", failure
        assert re.search(r"column top_urls: .*Array\(String\)", failure.llm_view()), (
            failure.llm_view()
        )

    async def test_local_time_is_refused_with_a_hint(self, dags: PumpDags) -> None:
        """LLM исправил массив, следующий отказ — время в поясе Москвы."""
        outcome = await dags.run(TRAFFIC_LOCAL_TIME_REFUSED)

        failure = outcome.failure("pg")
        assert failure.error_kind == "TransferError", failure
        assert re.search(
            r"column last_seen_msk: .*toDateTime64\(x, 3, 'UTC'\)", failure.llm_view()
        ), failure.llm_view()

    async def test_fallback_lands_text(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        report = await _landed(dags, source, target, TRAFFIC_FALLBACK)
        landed = Loaded(target, DW, "traffic")
        types: dict[str, str] = {}
        for name, kind, _ in await landed.columns():
            types[name] = kind

        assert "rows loaded" in report
        assert types["top_urls"] == "character varying"
        assert types["last_seen_msk"] == "character varying"
        assert types["views"] == "numeric(20,0)"
        assert types["p50_ms"] == "bigint"
        assert types["avg_ms"] == "double precision"
        assert await landed.aggregate("sum(views)") == VIEWS
        await self.rows_match(source, target, *self.ROW)
        await self.rows_match(
            source,
            target,
            "day::text || ' ' || device || ' ' || top_urls || ' ' || last_seen_msk",
            "concat(toString(day), ' ', toString(device), ' ', toString(top_urls), "
            "' ', toString(last_seen_msk))",
        )

    async def test_rewritten_query_lands_json_and_local_time(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        report = await _landed(dags, source, target, TRAFFIC_REWRITTEN)
        landed = Loaded(target, DW, "traffic")
        types: dict[str, str] = {}
        for name, kind, _ in await landed.columns():
            types[name] = kind

        assert "rows loaded" in report
        assert types["top_urls"] == json_type(target)
        assert types["last_seen_msk"] == "timestamp(3) without time zone"
        await self.rows_match(source, target, *self.ROW)
        await self.rows_match(
            source,
            target,
            "day::text || ' ' || device || ' ' "
            "|| to_char(last_seen_msk, 'YYYY-MM-DD HH24:MI:SS.MS')",
            "concat(toString(day), ' ', toString(device), ' ', "
            "toString(last_seen_msk))",
        )
        landed_urls = await Loaded(target, DW, "traffic").scalars(
            "top_urls::text", order_by="day"
        )
        source_urls = await ChLoaded(source, "traffic").column(
            "toJSONString(top_urls)", order_by="day"
        )

        assert sorted(map(str, parsed_json(landed_urls))) == sorted(
            map(str, parsed_json(source_urls))
        )


class TestArrow:
    """Тот же отчёт потоком arrow: LLM приводит Enum, LowCardinality и UUID к
    строке, тип uuid объявляет у приёмника, а String просит отдать строкой
    Arrow (по умолчанию ClickHouse пишет её binary, и приёмник отказывает)."""

    async def test_arrow_lands_the_report(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT_ARROW)
        landed = Loaded(target, DW, "orders_report_arrow")
        view = ChLoaded(source, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed.count() == ORDERS
        assert await landed.aggregate("sum(gross)") == await view.scalar("sum(gross)")
        assert await landed.aggregate("sum(paid)") == await view.scalar("sum(paid)")
        assert await landed.scalars("order_id", order_by="order_id") == (
            await view.column("order_id", order_by="order_id")
        )
        for name in ("status", "customer_uid", "customer_name", "city"):
            assert await landed.texts(name, order_by="order_id") == (
                await view.column(f"toString({name})", order_by="order_id")
            ), f"column {name} differs"


class TestBackToClickHouse:
    """Обратный путь: агрегат «город × месяц» с приёмника в ClickHouse парой
    postgres -> ClickHouse по tsv; суммы совпадают с отчётом источника."""

    async def test_city_month_comes_back(
        self, dags: PumpDags, source: ClickHouseSide, target: PostgresSide
    ) -> None:
        outcome = await dags.run(CITY_MONTH)
        report = outcome.node("ch").content
        print(f"\n--- {target.source.name} -> {source.source.name} ---")
        print(report)

        assert outcome.ok(), report

        city_month = ChLoaded(source, "city_month")
        view = ChLoaded(source, "orders_report")

        assert f"{await view.scalar('uniqExact(city, month)')} rows loaded" in report
        assert await city_month.types() == [
            ("city", "String"),
            ("month", "Date32"),
            ("orders", "Nullable(Int64)"),
            ("gross", "Nullable(Decimal(18, 2))"),
            ("balance", "Nullable(Decimal(18, 2))"),
        ]
        assert await city_month.scalar("sum(orders)") == ORDERS
        assert await city_month.scalar("sum(gross)") == await view.scalar("sum(gross)")
        assert await city_month.scalar("sum(balance)") == await view.scalar(
            "sum(balance)"
        )


async def test_rows_reached_every_column(source: ClickHouseSide) -> None:
    """Сторож данных стенда: в отчёте есть и NULL левого join, и частичные
    оплаты, и заметки с табуляцией — иначе сценарии выше ничего не ловят."""
    view = ChLoaded(source, "orders_report")

    assert await view.scalar("countIf(paid is null)") > 0
    assert await view.scalar("countIf(payment_state = 'partial')") > 0
    assert await view.scalar("countIf(position(note, '\\t') > 0)") > 0
    assert await view.scalar("countIf(last_ip is null)") > 0
