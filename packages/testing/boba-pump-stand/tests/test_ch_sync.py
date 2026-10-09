# ruff: noqa: S608, E501
"""Приёмник ch_stream_in на потоке arrow: pg_stream_out и ch_stream_out в каждый
ClickHouse стенда.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Источник — новейший postgres с таблицей всех ходовых типов: целые, numeric,
float, bool, text и varchar, date, timestamp и timestamptz, uuid, jsonb,
inet, interval, bytea, массив. Сценарии идут по порядку на каждом
ClickHouse и опираются на таблицы предыдущих:
    - создание: родные типы ClickHouse, json/inet/interval/bytea — String;
    - повторная загрузка с truncate: прежняя версия остаётся в двойнике __ex;
    - delete_where и delete nothing через двойник;
    - витрина со своими именами и типами через rename_columns и column_types;
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап;
    - шаблон create table с {database}, engine и order by;
    - тип без пары у ClickHouse: отказ и String по fallback_as_varchar;
    - сухой прогон;
    - круг ClickHouse -> ClickHouse через ch_stream_out;
    - база не Atomic — отказ до любого DDL.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

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
from boba.toolkit.result import SqlFailureResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = StandNames().of("pump_ch_sync")
CH_DATABASE = StandNames().of("pump_ch_sync")
ROWS = 60
THIRD = ROWS // 3
SIXTH = ROWS // 6
STAND_CLUSTER = "stand"

SOURCE_DDL = f"""
create table {PG_SCHEMA}.src (
    id bigint not null,
    i4 integer,
    n numeric(18,4),
    f8 double precision,
    b boolean,
    t text,
    vc varchar(20) not null,
    d date,
    ts timestamp(6),
    tz timestamptz,
    u uuid,
    j jsonb,
    ip inet,
    iv interval,
    by bytea
)"""

SOURCE_FILL = f"""
insert into {PG_SCHEMA}.src
select g,
       case when g % 7 = 0 then null else g * 3 end,
       case when g % 11 = 0 then null else (g * 1.2345)::numeric(18,4) end,
       case when g % 13 = 0 then null else g / 7.0 end,
       case when g % 5 = 0 then null else g % 2 = 0 end,
       case when g % 9 = 0 then null else 'text ' || g || E' \\t tab' end,
       'label ' || g % 50,
       date '2024-01-01' + g % 400,
       timestamp '2024-01-01 00:00:00.123456' + (g || ' minutes')::interval,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' seconds')::interval,
       md5(g::text)::uuid,
       jsonb_build_object('g', g, 'tag', 'x' || g % 3),
       ('10.1.' || g % 256 || '.' || g % 250)::inet,
       (g % 90 || ' minutes')::interval,
       decode(lpad(to_hex(g), 8, '0'), 'hex')
from generate_series(1, {ROWS}) g"""

EXPECTED_TYPES = [
    ("id", "Int64"),
    ("i4", "Nullable(Int32)"),
    ("n", "Nullable(Decimal(18, 4))"),
    ("f8", "Nullable(Float64)"),
    ("b", "Nullable(Bool)"),
    ("t", "Nullable(String)"),
    ("vc", "String"),
    ("d", "Nullable(Date32)"),
    ("ts", "Nullable(DateTime64(6))"),
    ("tz", "Nullable(DateTime64(6, 'UTC'))"),
    ("u", "Nullable(UUID)"),
    ("j", "Nullable(String)"),
    ("ip", "Nullable(String)"),
    ("iv", "Nullable(String)"),
    ("by", "Nullable(String)"),
]
"""Колонки ClickHouse после создания по нейтральному контракту: nullable
кроме объявленных not null, json/inet/interval/bytea — строками."""

ORDERS = """
name = "pg_to_ch_arrow_orders"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src where id <> 0"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "orders"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TWIN_CREATE = """
name = "pg_to_ch_arrow_twin_create"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "twin"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TWIN_TRUNCATE = """
name = "pg_to_ch_arrow_twin_truncate"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "twin"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TWIN_DELETE_WHERE = """
name = "pg_to_ch_arrow_twin_delete_where"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src where id <= $third"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "twin"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "id <= $third" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TWIN_APPEND = """
name = "pg_to_ch_arrow_twin_append"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src where id <= 10"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "twin"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

MART = """
name = "pg_to_ch_arrow_mart"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, vc, n, tz from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "mart"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { order_id = "id", label = "vc", amount = "n", moment = "tz" }, column_types = { amount = "Nullable(Decimal(20, 4))" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

DECLARED_NULLABLE = """
name = "pg_to_ch_arrow_declared_nullable"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, t, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "declared"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { n = "Decimal(20, 4)", t = "LowCardinality(String)", vc = "LowCardinality(String)" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

NULL_INTO_PLAIN = """
name = "pg_to_ch_arrow_null_into_plain"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i4 from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "plain"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

DRIFT_CREATE = """
name = "pg_to_ch_arrow_drift_create"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "drift"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

DRIFT_CHECK = """
name = "pg_to_ch_arrow_drift_check"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "drift"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

DRIFT_BACKUP = """
name = "pg_to_ch_arrow_drift_backup"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "drift"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TEMPLATED = """
name = "pg_to_ch_arrow_templated"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "templated"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = ReplacingMergeTree order by {order_by} settings index_granularity = 1024"
order_by = "id"
feed = "rows"
"""

TEMPLATED_BAD = """
name = "pg_to_ch_arrow_templated_bad"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "templated_bad"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) engine = MergeTree order by tuple()"
order_by = "tuple()"
feed = "rows"
"""

ARRAYS_REFUSED = """
name = "pg_to_ch_arrow_arrays_refused"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, vc, array[id, id + 1]::int[] as arr from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "arrays"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

ARRAYS_AS_STRING = """
name = "pg_to_ch_arrow_arrays_as_string"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, vc, array[id, id + 1]::int[] as arr from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "arrays"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

DRY_RUN = """
name = "pg_to_ch_arrow_dry_run"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "orders"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "nothing" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

CIRCLE = """
name = "ch_to_ch_arrow_circle"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, i4, n, f8, t, vc, d, ts, tz, toString(u) as u from $ch_database.orders settings output_format_arrow_string_as_string = 1"
wire = "arrow"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "circle"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

TYPED_COPY_CREATE = """
name = "ch_to_ch_tsv_typed_copy_create"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.typed order by id"
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_copy"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

TYPED_COPY_RELOAD = """
name = "ch_to_ch_tsv_typed_copy_reload"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.typed order by id"
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_copy"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

TYPED_COPY_CHECK = """
name = "ch_to_ch_tsv_typed_copy_check"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.typed order by id"
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_copy"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

TYPED_WIDER_CHECK = """
name = "ch_to_ch_tsv_typed_wider_check"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.typed order by id"
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_wider"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

TYPED_NARROW_CHECK = """
name = "ch_to_ch_tsv_typed_narrow_check"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.typed order by id"
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_narrow"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

TYPED_MART = """
name = "ch_to_ch_tsv_typed_mart"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.typed order by id"
wire = "tsv"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_mart"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { key = "id", label = "lc" }, column_types = { label = "String", big = "Decimal(76, 10)" } }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "key"
feed = "rows"
"""

TSV_COLUMNS_REFUSED = """
name = "ch_to_ch_tsv_columns_refused"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id from $ch_database.typed"
wire = "tsv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "typed_refused"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

ORDINARY = """
name = "pg_to_ch_arrow_ordinary_database"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "${ch_database}_ordinary"
table_name = "orders"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

REPLICATED_NO_CLUSTER = """
name = "pg_to_ch_arrow_replicated_no_cluster"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "replicated"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
order_by = "id"
feed = "rows"
"""

REPLICATED_BAD_CLUSTER = """
name = "pg_to_ch_arrow_replicated_bad_cluster"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "replicated"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
cluster = "nope"
order_by = "id"
feed = "rows"
"""

REPLICATED_CREATE = """
name = "pg_to_ch_arrow_replicated_create"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "replicated"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
cluster = "stand"
order_by = "id"
feed = "rows"
"""

REPLICATED_DELETE_WHERE = """
name = "pg_to_ch_arrow_replicated_delete_where"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.src where id <= $sixth"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "replicated"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "id <= $sixth" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
cluster = "stand"
order_by = "id"
feed = "rows"
"""


def _newest_postgres() -> Any:
    newest: Any = None
    for source in STAND.sources:
        if source.name.startswith("pg-"):
            newest = source

    if newest is None:
        raise AssertionError("no postgres source on the stand")

    return newest


@pytest.fixture(scope="module")
async def postgres() -> AsyncIterator[PostgresSide]:
    side = PostgresSide(_newest_postgres(), PG_SCHEMA)
    await side.connect()
    await side.recreate_schema([SOURCE_DDL, SOURCE_FILL])

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


@pytest.fixture
def dags(
    tmp_path: Path, postgres: PostgresSide, clickhouse: ClickHouseSide
) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"pg": postgres.profile, "ch": clickhouse.profile},
        {
            "pg_schema": PG_SCHEMA,
            "ch_database": CH_DATABASE,
            "third": str(THIRD),
            "sixth": str(SIXTH),
        },
    )


@pytest.fixture
def ch_dags(tmp_path: Path, clickhouse: ClickHouseSide) -> PumpDags:
    """Круг ClickHouse -> ClickHouse: соединение одно."""
    return PumpDags(tmp_path, {"ch": clickhouse.profile}, {"ch_database": CH_DATABASE})


async def _landed(dags: PumpDags, clickhouse: ClickHouseSide, spec: str) -> str:
    """Запуск описания; отчёт приёмника — узла ch."""
    outcome = await dags.run(spec)
    report = outcome.node("ch").content
    print(f"\n--- {clickhouse.source.name}: {outcome.dag} ---\n{report}")

    assert outcome.ok(), report

    return report


class TestCreate:
    async def test_types_and_values_land(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, ORDERS)
        landed = ChLoaded(clickhouse, "orders")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert await landed.types() == EXPECTED_TYPES
        assert await landed.count() == ROWS
        assert Decimal(str(await landed.scalar("sum(n)"))) == await source.aggregate(
            "sum(n)"
        )
        assert await landed.scalar("countIf(i4 is null)") == await source.aggregate(
            "count(*) filter (where i4 is null)"
        )
        assert await landed.column("t") == await source.texts("t")
        assert await landed.column("vc") == await source.texts("vc")
        assert await landed.column("j") == await source.texts("j")
        assert await landed.column("toString(u)") == await source.texts("u")
        assert await landed.column("toString(d)") == await source.texts("d")


class TestTwin:
    """Загрузка через двойник: truncate оставляет в __ex прежнюю версию,
    delete_where переносит в двойник строки вне условия, delete nothing —
    все прежние строки."""

    async def test_reload_keeps_the_previous_version_in_the_twin(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await _landed(dags, clickhouse, TWIN_CREATE)

        report = await _landed(dags, clickhouse, TWIN_TRUNCATE)

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "twin").count() == ROWS
        assert await ChLoaded(clickhouse, "twin__ex").count() == ROWS

    async def test_delete_where_replaces_only_the_matching_rows(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(
            f"alter table {CH_DATABASE}.twin update t = 'stale' where id <= {THIRD}",
            settings={"mutations_sync": 2},
        )

        report = await _landed(dags, clickhouse, TWIN_DELETE_WHERE)
        landed = ChLoaded(clickhouse, "twin")

        assert f"{THIRD} rows matching" in report
        assert f"{THIRD} rows loaded" in report
        assert await landed.count() == ROWS
        assert await landed.scalar("countIf(t = 'stale')") == 0
        assert await landed.column("t") == await Loaded(
            postgres, PG_SCHEMA, "src"
        ).texts("t")

    async def test_delete_nothing_appends(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, TWIN_APPEND)

        assert "10 rows loaded" in report
        assert await ChLoaded(clickhouse, "twin").count() == ROWS + 10


class TestRenamedMart:
    """Витрина ClickHouse со своими именами и типами: rename_columns связывает
    колонки с полями потока, column_types даёт тип витрины, вставка идёт
    через input() с переименованием в select."""

    MART: ClassVar[str] = (
        f"create table {CH_DATABASE}.mart (order_id Int64, "
        "label LowCardinality(String), amount Nullable(Decimal(20, 4)), "
        "moment Nullable(DateTime64(6, 'UTC'))) "
        "engine = MergeTree order by order_id"
    )

    async def test_missing_mart_is_refused(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.mart")

        outcome = await dags.run(MART)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert "table is missing" in failure.llm_view()

    async def test_mart_is_filled_by_its_own_names(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(self.MART)
        source = Loaded(postgres, PG_SCHEMA, "src")

        report = await _landed(dags, clickhouse, MART)
        mart = ChLoaded(clickhouse, "mart")

        assert f"{ROWS} rows loaded" in report
        assert await mart.count() == ROWS
        assert await mart.scalar("sum(order_id)") == await source.aggregate("sum(id)")
        assert Decimal(str(await mart.scalar("sum(amount)"))) == (
            await source.aggregate("sum(n)")
        )
        assert await mart.scalar("uniqExact(label)") == await source.aggregate(
            "count(distinct vc)"
        )


class TestNulls:
    """NULL решает поток, а не объявленный тип: объявленный тип без Nullable у
    nullable колонки получает Nullable снаружи (у LowCardinality — внутри);
    NULL в колонку без Nullable существующей таблицы — ошибка сервера, не
    значение по умолчанию, таблица не меняется."""

    async def test_declared_type_of_a_nullable_column_gets_nullable(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await _landed(dags, clickhouse, DECLARED_NULLABLE)
        types = dict(await ChLoaded(clickhouse, "declared").types())

        assert types["n"] == "Nullable(Decimal(20, 4))"
        assert types["t"] == "LowCardinality(Nullable(String))"
        assert types["vc"] == "LowCardinality(String)"

    async def test_null_into_a_plain_column_is_refused(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.plain")
        await clickhouse.command(
            f"create table {CH_DATABASE}.plain (id Int64, i4 Int32) "
            "engine = MergeTree order by id"
        )
        await clickhouse.command(f"insert into {CH_DATABASE}.plain values (0, 0)")

        outcome = await dags.run(NULL_INTO_PLAIN)

        failure = outcome.failure("ch")
        assert isinstance(failure, SqlFailureResult), failure
        assert failure.statements[-1].status == "failed: ClickHouseQueryError"
        assert "NULL" in failure.llm_view()
        assert await ChLoaded(clickhouse, "plain").count() == 1


class TestSchemaDrift:
    async def test_drift_is_refused_then_backed_up(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await _landed(dags, clickhouse, DRIFT_CREATE)
        await clickhouse.command(
            f"alter table {CH_DATABASE}.drift modify column i4 Nullable(Int16)",
            settings={"mutations_sync": 2},
        )

        refused = await dags.run(DRIFT_CHECK)

        failure = refused.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert "i4" in failure.llm_view()

        report = await _landed(dags, clickhouse, DRIFT_BACKUP)
        drift = ChLoaded(clickhouse, "drift")
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "drift_bak_" in report
        assert len(backups) == 1
        assert await ChLoaded(clickhouse, backups[0]).count() == ROWS
        assert ("i4", "Nullable(Int32)") in await drift.types()
        assert await drift.count() == ROWS


class TestCreateTemplate:
    async def test_engine_and_order_come_from_the_template(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, TEMPLATED)
        table = ChLoaded(clickhouse, "templated")
        engine = await table.engine()

        assert f"{ROWS} rows loaded" in report
        assert engine == "ReplacingMergeTree"
        assert await table.sorting_key() == "id"
        assert await table.count() == ROWS

    async def test_template_with_schema_name_is_refused(self, dags: PumpDags) -> None:
        outcome = await dags.run(TEMPLATED_BAD)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert (
            "create_table uses {schema_name}, but the receiver has no such"
            in failure.llm_view()
        )


class TestUnknownTypes:
    """Массив postgres в нейтральном контракте — семейство array, пары у
    ClickHouse нет: ошибка с подсказкой, по fallback_as_varchar — String с
    текстом массива postgres."""

    async def test_array_is_refused_by_default(self, dags: PumpDags) -> None:
        outcome = await dags.run(ARRAYS_REFUSED)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert "column arr: the target clickhouse" in failure.llm_view()

    async def test_array_lands_as_string_on_fallback(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await _landed(dags, clickhouse, ARRAYS_AS_STRING)
        table = ChLoaded(clickhouse, "arrays")

        assert ("arr", "Nullable(String)") in await table.types()
        assert (await table.column("arr"))[:2] == ["{1,2}", "{2,3}"]


class TestDryRun:
    async def test_nothing_changes(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        table = ChLoaded(clickhouse, "orders")
        types = await table.types()

        report = await _landed(dags, clickhouse, DRY_RUN)

        assert report.startswith("0 rows loaded")
        assert await table.types() == types
        assert await table.count() == ROWS


class TestClickHouseCircle:
    """ClickHouse -> ClickHouse: ch_stream_out читает загруженную таблицу потоком
    arrow, ch_stream_in кладёт её в новую. UUID ClickHouse до 26 в Arrow не
    выгружает, поэтому запрос отдаёт его toString, как советует etl_skill."""

    async def test_orders_come_around(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(ch_dags, clickhouse, CIRCLE)
        circle = ChLoaded(clickhouse, "circle")
        orders = ChLoaded(clickhouse, "orders")

        assert f"{ROWS} rows loaded" in report
        assert await circle.count() == ROWS
        assert await circle.scalar("sum(n)") == await orders.scalar("sum(n)")
        assert await circle.column("t") == await orders.column("t")
        assert await circle.column("toString(u)") == await orders.column("toString(u)")
        assert await circle.column("toString(tz)") == await orders.column(
            "toString(tz)"
        )


class TestTsvCircle:
    """ClickHouse -> ClickHouse по tsv: контракт из шапки того же запроса,
    типы ClickHouse едут как есть — LowCardinality, DateTime64 с поясом,
    Enum8, FixedString, Array, Map, Decimal с широкой точностью, IPv6, UUID —
    и таблица создаётся с теми же типами; сверка с существующей таблицей по
    текстам типов."""

    TYPED: ClassVar[str] = f"""
        create table {CH_DATABASE}.typed (
            id Int64,
            lc LowCardinality(String),
            dt DateTime64(3, 'Europe/Moscow'),
            en Enum8('a' = 1, 'b' = 2),
            fs FixedString(4),
            arr Array(Nullable(Int32)),
            m Map(String, UInt64),
            big Nullable(Decimal(60, 10)),
            ip IPv6,
            u UUID
        ) engine = MergeTree order by id"""
    FILL: ClassVar[str] = f"""
        insert into {CH_DATABASE}.typed
        select number, concat('lc', toString(number % 3)),
               toDateTime64('2024-02-29 13:14:15.123', 3, 'Europe/Moscow') + number,
               if(number % 2 = 0, 'a', 'b'), toFixedString('ab', 4),
               [toNullable(toInt32(number)), null],
               map('k', toUInt64(number)),
               if(number % 5 = 0, null, toDecimal256(number, 10) / 7),
               toIPv6('2001:db8::1'), generateUUIDv4()
        from numbers({ROWS})"""
    COLUMNS: ClassVar[dict[str, str]] = {
        "id": "id Int64",
        "lc": "lc LowCardinality(String)",
        "dt": "dt DateTime64(3, 'Europe/Moscow')",
        "fs": "fs FixedString(4)",
        "arr": "arr Array(Nullable(Int32))",
        "big": "big Nullable(Decimal(60, 10))",
    }
    """Объявления колонок typed по имени: тест «уже» подменяет одно из них."""
    EXPRESSIONS: ClassVar[tuple[str, ...]] = (
        "lc",
        "toString(dt)",
        "toString(en)",
        "hex(fs)",
        "toString(arr)",
        "toString(m)",
        "toString(big)",
        "toString(ip)",
        "toString(u)",
    )

    async def test_types_travel_as_they_are(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed")
        await clickhouse.command(self.TYPED)
        await clickhouse.command(self.FILL)
        typed = ChLoaded(clickhouse, "typed")

        report = await _landed(ch_dags, clickhouse, TYPED_COPY_CREATE)
        copy = ChLoaded(clickhouse, "typed_copy")

        assert f"{ROWS} rows loaded" in report
        assert await copy.types() == await typed.types()
        assert await copy.count() == ROWS
        for expression in self.EXPRESSIONS:
            assert await copy.column(expression) == await typed.column(expression), (
                expression
            )

    async def test_reload_passes_the_check_by_type_texts(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(ch_dags, clickhouse, TYPED_COPY_RELOAD)

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "typed_copy").count() == ROWS

    async def test_type_drift_is_refused(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(
            f"alter table {CH_DATABASE}.typed_copy modify column big Nullable(Float64)",
            settings={"mutations_sync": 2},
        )

        outcome = await ch_dags.run(TYPED_COPY_CHECK)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert "big: type differs" in failure.llm_view()

    WIDER: ClassVar[str] = f"""
        create table {CH_DATABASE}.typed_wider (
            id Int128,
            lc String,
            dt DateTime64(6, 'Europe/Moscow'),
            en String,
            fs FixedString(8),
            arr Array(Nullable(Int32)),
            m Map(String, UInt64),
            big Nullable(Decimal(76, 12)),
            ip IPv6,
            u UUID
        ) engine = MergeTree order by id"""

    async def test_wider_table_takes_the_stream_with_warnings(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed_wider")
        await clickhouse.command(self.WIDER)

        report = await _landed(ch_dags, clickhouse, TYPED_WIDER_CHECK)
        wider = ChLoaded(clickhouse, "typed_wider")
        typed = ChLoaded(clickhouse, "typed")

        assert f"{ROWS} rows loaded" in report
        assert '"column": "id"' in report
        assert "table Int128 is wider than stream Int64" in report
        assert "table Decimal(76, 12) is wider" in report
        assert "table DateTime64(6, 'Europe/Moscow') is wider" in (report)
        assert await wider.count() == ROWS
        assert await wider.column("toString(big)") == await typed.column(
            "toString(toDecimal256(big, 12))"
        )
        assert await wider.column("toString(dt)") == await typed.column(
            "toString(toDateTime64(dt, 6, 'Europe/Moscow'))"
        )

    @pytest.mark.parametrize(
        ("column", "table_type", "message"),
        [
            ("id", "Int32", "table Int32 is narrower than stream Int64"),
            ("id", "UInt64", "table UInt64 has no sign, stream Int64 is signed"),
            (
                "big",
                "Nullable(Decimal(60, 8))",
                "table Decimal\\(60, 8\\) truncates the scale",
            ),
            (
                "big",
                "Nullable(Decimal(20, 10))",
                "table Decimal\\(20, 10\\) holds fewer",
            ),
            ("dt", "DateTime64(3, 'UTC')", "time zone differs"),
            (
                "dt",
                "DateTime64(0, 'Europe/Moscow')",
                "table DateTime64\\(0, 'Europe/Moscow'\\) is coarser",
            ),
            ("fs", "FixedString(2)", "table FixedString\\(2\\) is narrower"),
            ("lc", "Enum8('x' = 1)", "type differs"),
            ("arr", "Array(Int32)", "type differs"),
        ],
    )
    async def test_narrower_table_is_refused(
        self,
        ch_dags: PumpDags,
        clickhouse: ClickHouseSide,
        column: str,
        table_type: str,
        message: str,
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed_narrow")
        await clickhouse.command(
            self.TYPED.replace("typed", "typed_narrow").replace(
                self.COLUMNS[column], f"{column} {table_type}"
            )
        )

        outcome = await ch_dags.run(TYPED_NARROW_CHECK)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert re.search(f"{column}: {message}", failure.llm_view()), failure.llm_view()

    async def test_renamed_and_retyped_columns(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(ch_dags, clickhouse, TYPED_MART)
        types = dict(await ChLoaded(clickhouse, "typed_mart").types())

        assert f"{ROWS} rows loaded" in report
        assert types["key"] == "Int64"
        assert types["label"] == "String"
        assert types["big"] == "Nullable(Decimal(76, 10))"

    async def test_columns_are_refused_with_tsv(self, ch_dags: PumpDags) -> None:
        outcome = await ch_dags.run(TSV_COLUMNS_REFUSED)

        failure = outcome.failure("src")
        assert failure.error_kind == "TransferError", failure
        assert "columns apply to wire arrow" in failure.llm_view()


class TestOrdinaryDatabase:
    async def test_non_atomic_database_is_refused(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        ordinary = f"{CH_DATABASE}_ordinary"
        await clickhouse.command(
            f"create database if not exists {ordinary} engine = Ordinary",
            settings={"allow_deprecated_database_ordinary": 1},
        )

        try:
            outcome = await dags.run(ORDINARY)

            failure = outcome.failure("ch")
            assert failure.error_kind == "TransferError", failure
            assert "needs an Atomic database" in failure.llm_view()
        finally:
            await clickhouse.command(f"drop database if exists {ordinary}")


class TestReplicated:
    """Шаблон по умолчанию — ReplicatedMergeTree: сервер создаёт его только
    on cluster, путь в Keeper из default_replica_path с {uuid}. Проверяется
    на ClickHouse с Keeper и кластером stand из одного узла; двойник и
    exchange tables тоже идут on cluster."""

    @pytest.fixture(autouse=True)
    async def keeper(self, clickhouse: ClickHouseSide) -> None:
        clusters = await ChLoaded(clickhouse, "").clusters()

        assert STAND_CLUSTER in clusters, (
            f"{clickhouse.source.name}: the stand server needs Keeper and the "
            f"cluster {STAND_CLUSTER!r} (conf/keeper.xml of the clickhouse stand), "
            f"got clusters {clusters}"
        )

    async def test_default_template_needs_a_cluster(self, dags: PumpDags) -> None:
        outcome = await dags.run(REPLICATED_NO_CLUSTER)

        failure = outcome.failure("ch")
        assert isinstance(failure, SqlFailureResult), failure
        assert failure.statements[-1].status == "failed: ClickHouseQueryError"
        assert "ON CLUSTER" in failure.llm_view()

    async def test_unknown_cluster_is_refused_before_ddl(self, dags: PumpDags) -> None:
        outcome = await dags.run(REPLICATED_BAD_CLUSTER)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert "cluster 'nope' is not in" in failure.llm_view()

    async def test_replicated_table_loads_through_the_twin(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        table = ChLoaded(clickhouse, "replicated")
        report = await _landed(dags, clickhouse, REPLICATED_CREATE)

        assert f"{ROWS} rows loaded" in report
        assert await table.engine() == "ReplicatedMergeTree"
        assert await table.sorting_key() == "id"
        assert await table.count() == ROWS

        report = await _landed(dags, clickhouse, REPLICATED_DELETE_WHERE)
        twin = ChLoaded(clickhouse, "replicated__ex")

        assert f"{SIXTH} rows matching" in report
        assert await table.count() == ROWS
        assert await twin.engine() == "ReplicatedMergeTree"
        assert await twin.count() == ROWS
