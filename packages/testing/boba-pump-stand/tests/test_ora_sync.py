# ruff: noqa: S608, E501
"""Насосы Oracle на потоке arrow: ora_stream_out как источник и ora_stream_in
как приёмник со стратегиями, на каждом Oracle стенда.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Что проверяется:
    - круг Oracle -> Oracle: таблица всех ходовых типов уезжает
      ora_stream_out и приходит ora_stream_in в новую таблицу теми же типами
      (текст типа источника в DDL), значения совпадают; повторная загрузка
      проходит сверку;
    - Oracle -> postgres и Oracle -> ClickHouse: приёмник создаёт таблицу по
      нейтральному контракту, целый NUMBER — bigint/Int64, NUMBER(p, s) —
      numeric/Decimal, DATE — момент в секундах;
    - postgres -> Oracle и ClickHouse -> Oracle: приёмник создаёт таблицу
      типами Oracle: uuid и time — VARCHAR2, json — CLOB, boolean — BOOLEAN
      или NUMBER(1), date — DATE, bytea — BLOB;
    - стратегии приёмника: delete_where, витрина через rename и column_types,
      дрейф схемы с бэкапом, шаблон create table, сухой прогон, отказ на
      таблице уже потока;
    - отказы источника до выполнения: TIMESTAMP WITH TIME ZONE, INTERVAL,
      XMLTYPE — с подсказкой, что писать в select.
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
    OracleSide,
    OraLoaded,
    PostgresSide,
    PumpDags,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.stand.names import StandNames
from boba.toolkit.result import FailureResult, SqlFailureResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 60
ARRAYSIZE = 500
PG_SCHEMA = StandNames().of("pump_ora_sync")
CH_DATABASE = StandNames().of("pump_ora_sync")
BOOLEAN_SINCE = 23
WITHOUT_BOOLEAN = STAND.only("oracle", STAND.oracle_before(BOOLEAN_SINCE))
"""Отказ сырому bool — поведение серверов без типа BOOLEAN."""
SRC = StandNames().of("src")
OWNER = PumpUser().name
MONTH = "2024-03"

TYPED_DDL = [
    "id number(10) not null",
    "n19 number(19)",
    "n18_4 number(18,4)",
    "nfree number",
    "f126 float(126)",
    "bd binary_double",
    "bf binary_float",
    "vc varchar2(40)",
    "vcc varchar2(20 char)",
    "nvc nvarchar2(20)",
    "c5 char(5)",
    "cl clob",
    "r16 raw(16)",
    "bl blob",
    "d date",
    "ts6 timestamp(6)",
    "ts9 timestamp(9)",
]
TYPED_FILL = f"""
insert into {OWNER}.{SRC}
select level,
       level * 1000000007,
       level / 7,
       level * 3,
       1 / 3,
       level / 7,
       level / 3,
       'v' || level || ' tab' || chr(9) || 'quote "q" \\ back',
       case when mod(level, 5) = 0 then null else 'c' || level end,
       'n' || level,
       'ab',
       to_clob(rpad('long ' || level, 5000, 'x')),
       hextoraw('00ff' || lpad(to_char(level, 'fmxxxx'), 4, '0')),
       to_blob(hextoraw('00ff10')),
       date '2024-01-01' + mod(level, 400),
       timestamp '2024-02-29 13:14:15.123456' + numtodsinterval(level, 'second'),
       timestamp '2024-02-29 13:14:15.123456789' + numtodsinterval(level, 'second')
from dual connect by level <= {ROWS}"""

EXPECTED_ORACLE = [
    ("id", "NUMBER(10,0)", True),
    ("n19", "NUMBER(19,0)", False),
    ("n18_4", "NUMBER(18,4)", False),
    ("nfree", "NUMBER", False),
    ("f126", "FLOAT(126)", False),
    ("bd", "BINARY_DOUBLE", False),
    ("bf", "BINARY_FLOAT", False),
    ("vc", "VARCHAR2(40)", False),
    ("vcc", "VARCHAR2(20 CHAR)", False),
    ("nvc", "NVARCHAR2(20)", False),
    ("c5", "CHAR(5)", False),
    ("r16", "RAW(16)", False),
    ("d", "DATE", False),
    ("ts6", "TIMESTAMP(6)", False),
    ("ts9", "TIMESTAMP(9)", False),
    ("cl", "CLOB", False),
    ("bl", "BLOB", False),
]
"""Круг Oracle -> Oracle: типы источника как есть; VARCHAR2(20 CHAR) на
однобайтовой базе становится VARCHAR2(20)."""

EXPECTED_POSTGRES = [
    ("id", "bigint", True),
    ("n19", "numeric(19,0)", False),
    ("n18_4", "numeric(18,4)", False),
    ("nfree", "numeric(38,0)", False),
    ("f126", "double precision", False),
    ("bd", "double precision", False),
    ("bf", "real", False),
    ("vc", "character varying(40)", False),
    ("vcc", "character varying(20)", False),
    ("nvc", "character varying(20)", False),
    ("c5", "character varying(5)", False),
    ("r16", "character varying(32)", False),
    ("d", "timestamp(0) without time zone", False),
    ("ts6", "timestamp(6) without time zone", False),
    ("ts9", "timestamp(6) without time zone", False),
    ("cl", "text", False),
]

EXPECTED_CLICKHOUSE = {
    "id": "Int64",
    "n19": "Nullable(Decimal(19, 0))",
    "n18_4": "Nullable(Decimal(18, 4))",
    "nfree": "Nullable(Decimal(38, 0))",
    "f126": "Nullable(Float64)",
    "bd": "Nullable(Float64)",
    "bf": "Nullable(Float32)",
    "vc": "Nullable(String)",
    "c5": "Nullable(String)",
    "r16": "Nullable(String)",
    "d": "Nullable(DateTime64(0))",
    "ts6": "Nullable(DateTime64(6))",
    "ts9": "Nullable(DateTime64(9))",
    "cl": "Nullable(String)",
    "bl": "Nullable(String)",
}

COMPARED_ORACLE = {
    "vc": "vc",
    "vcc": "vcc",
    "c5": "c5",
    "n18_4": "to_char(n18_4)",
    "nfree": "to_char(nfree)",
    "f126": "to_char(cast(f126 as binary_double))",
    "bd": "to_char(bd)",
    "r16": "rawtohex(r16)",
    "d": "to_char(d, 'yyyy-mm-dd hh24:mi:ss')",
    "ts6": "to_char(ts6, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    "ts9": "to_char(ts9, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    "cl": "dbms_lob.getlength(cl)",
    "bl": "rawtohex(dbms_lob.substr(bl, 3, 1))",
}
"""Опорные выражения круга: значение текстом на обеих таблицах Oracle;
FLOAT(126) драйвер читает как double, TIMESTAMP(9) — до микросекунд:
сравниваются double и микросекунды."""

CIRCLE = """
name = "ora_circle"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", f126 as "f126", bd as "bd", bf as "bf", vc as "vc", vcc as "vcc", nvc as "nvc", c5 as "c5", r16 as "r16", d as "d", ts6 as "ts6", ts9 as "ts9", cl as "cl", bl as "bl" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "circle"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

CIRCLE_RELOAD = """
name = "ora_circle_reload"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", f126 as "f126", bd as "bd", bf as "bf", vc as "vc", vcc as "vcc", nvc as "nvc", c5 as "c5", r16 as "r16", d as "d", ts6 as "ts6", ts9 as "ts9", cl as "cl", bl as "bl" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "circle"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

TO_POSTGRES = """
name = "ora_to_postgres"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", f126 as "f126", bd as "bd", bf as "bf", vc as "vc", vcc as "vcc", nvc as "nvc", c5 as "c5", rawtohex(r16) as "r16", d as "d", ts6 as "ts6", ts9 as "ts9", cl as "cl" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "$from_table"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""
"""Для postgres: двоичные колонки текстом hex — COPY из arrow bytes не берёт."""

TO_CLICKHOUSE = """
name = "ora_to_clickhouse"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", f126 as "f126", bd as "bd", bf as "bf", vc as "vc", c5 as "c5", r16 as "r16", d as "d", ts6 as "ts6", ts9 as "ts9", cl as "cl", bl as "bl" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "$from_table"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

PG_RAW_BOOL = """
name = "pg_to_ora_raw_bool"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, b from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_pg"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

PG_ARRAY_REFUSED = """
name = "pg_to_ora_array_refused"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, arr from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_pg"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

PG_ARRAY_AS_CLOB = """
name = "pg_to_ora_array_as_clob"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, arr from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_pg"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

PG_EVERY_FAMILY = """
name = "pg_to_ora_every_family"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, n, f8, $pg_bool, t, vc, bin, d, ts, tz, tm, u, j, ip, iv from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_pg"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

PG_RELOAD = """
name = "pg_to_ora_reload"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, n, f8, $pg_bool, t, vc, bin, d, ts, tz, tm, u, j, ip, iv from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_pg"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

PG_MOMENTS = """
name = "pg_to_ora_moments"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, ts from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "moments"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

PG_MOMENTS_ZONED = """
name = "pg_to_ora_moments_zoned"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, tz as ts from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "moments"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

SLICES = """
name = "pg_to_ora_slices"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, d, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "slices"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

SLICES_MONTH = """
name = "pg_to_ora_slices_month"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, d, vc from $pg_schema.src where to_char(d, 'YYYY-MM') = '$month'"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "slices"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "to_char(d, 'yyyy-mm') = '$month'" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

MART = """
name = "pg_to_ora_mart"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, vc, ts from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "mart"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { key = "id", amount = "n", label = "vc" }, column_types = { amount = "NUMBER(14,3)", label = "VARCHAR2(10)" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

NARROW = """
name = "pg_to_ora_narrow"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "narrow"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

DRIFT = """
name = "pg_to_ora_drift"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "drift"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

DRIFT_CHECKED = """
name = "pg_to_ora_drift_checked"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "drift"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

DRIFT_BACKED_UP = """
name = "pg_to_ora_drift_backed_up"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "drift"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

TEMPLATED = """
name = "pg_to_ora_templated"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "templated"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) tablespace users nologging"
after = ["update $ora_schema.templated set n = n * 2"]
feed = "rows"
"""

DRY_RUN = """
name = "pg_to_ora_dry_run"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "templated"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "nothing" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

FAILED_AFTER_STEP = """
name = "pg_to_ora_failed_after_step"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "templated"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
after = ["insert into no_such_table values (1)"]
feed = "rows"
"""

CH_RAW_UUID = """
name = "ch_to_ora_raw_uuid"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, u from $ch_database.src order by id settings output_format_arrow_string_as_string = 1"
wire = "arrow"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_ch"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

CH_EVERY_FAMILY = """
name = "ch_to_ora_every_family"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, u8, dec, f32, s, lc, d, dt, $ch_bool, toString(u) as u from $ch_database.src order by id settings output_format_arrow_string_as_string = 1"
wire = "arrow"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "from_ch"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

ZONED_TIMESTAMP = """
name = "ora_zoned_timestamp"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", from_tz(ts6, '+03:00') as "tstz" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "refused"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

UTC_WALL_TIME = """
name = "ora_utc_wall_time"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", sys_extract_utc(from_tz(ts6, '+03:00')) as "utc" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "utc"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { utc = "TIMESTAMP(6) WITH TIME ZONE" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

INTERVAL_DS = """
name = "ora_interval_ds"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", numtodsinterval(id, 'second') as "v" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "refused"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

INTERVAL_YM = """
name = "ora_interval_ym"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", numtoyminterval(id, 'month') as "v" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "refused"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

XMLTYPE = """
name = "ora_xmltype"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", xmltype('<a/>') as "v" from $ora_schema.$src'''
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "refused"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""

MISSING_DECLARATION = """
name = "ora_missing_declaration"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", f126 as "f126", bd as "bd", bf as "bf", vc as "vc", vcc as "vcc", nvc as "nvc", c5 as "c5", r16 as "r16", d as "d", ts6 as "ts6", ts9 as "ts9", cl as "cl", bl as "bl" from $ora_schema.$src'''
columns = [{ name = "nope", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "refused"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
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


def _newest_clickhouse() -> Any:
    return STAND.demo_clickhouse()[-1]


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[OracleSide]:
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    await side.create(SRC, TYPED_DDL)
    await side.run((TYPED_FILL,))

    yield side

    await side.drop()


@pytest.fixture(scope="module")
async def postgres() -> AsyncIterator[PostgresSide]:
    side = PostgresSide(_newest_postgres(), PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(_newest_clickhouse(), CH_DATABASE)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


def from_table(oracle: OracleSide) -> str:
    """Имя таблицы приёмника по имени Oracle стенда."""
    return f"from_{oracle.source.name}".replace("-", "_").replace(".", "_")


def pg_bool(oracle: OracleSide) -> str:
    """bool до 23 драйвер в Oracle не кладёт: LLM шлёт его числом."""
    if oracle.version < BOOLEAN_SINCE:
        return "b::int as b"

    return "b"


def ch_bool(oracle: OracleSide) -> str:
    if oracle.version < BOOLEAN_SINCE:
        return "toUInt8(b) as b"

    return "b"


@pytest.fixture
def dags(
    tmp_path: Path,
    oracle: OracleSide,
    postgres: PostgresSide,
    clickhouse: ClickHouseSide,
) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"ora": oracle.profile, "pg": postgres.profile, "ch": clickhouse.profile},
        {
            "ora_schema": OWNER,
            "src": SRC,
            "pg_schema": PG_SCHEMA,
            "ch_database": CH_DATABASE,
            "from_table": from_table(oracle),
            "pg_bool": pg_bool(oracle),
            "ch_bool": ch_bool(oracle),
            "month": MONTH,
        },
    )


async def _landed(dags: PumpDags, oracle: OracleSide, spec: str) -> str:
    """Запуск описания; отчёт приёмника — узла dst."""
    outcome = await dags.run(spec)
    report = outcome.node("dst").content
    print(f"\n--- {oracle.source.name}: {outcome.dag} ---\n{report}")

    assert outcome.ok(), report

    return report


async def _refused(
    dags: PumpDags, oracle: OracleSide, spec: str, key: str
) -> FailureResult:
    """Запуск описания, которое обязано сорваться на узле key: его отказ."""
    outcome = await dags.run(spec)
    failure = outcome.failure(key)
    print(
        f"\n--- {oracle.source.name}: {outcome.dag} refused ---\n{failure.llm_view()}"
    )

    return failure


def expected_oracle(oracle: OracleSide) -> list[tuple[str, str, bool]]:
    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_ORACLE:
        text = kind
        if name == "vcc" and not oracle.unicode:
            text = "VARCHAR2(20)"

        columns.append((name, text, not_null))

    return columns


async def same_values(oracle: OracleSide, table: str) -> None:
    source = OraLoaded(oracle, SRC)
    landed = OraLoaded(oracle, table)
    for name, _, _ in EXPECTED_ORACLE:
        expression = COMPARED_ORACLE.get(name, name)
        expected = await source.column(expression)
        actual = await landed.column(expression)

        assert actual == expected, f"column {name} differs"


class TestOracleCircle:
    async def test_types_survive_the_circle(
        self, dags: PumpDags, oracle: OracleSide
    ) -> None:
        report = await _landed(dags, oracle, CIRCLE)
        landed = OraLoaded(oracle, "circle")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == expected_oracle(oracle)
        assert await landed.count() == ROWS
        assert await landed.scalar("count(vcc)") == await OraLoaded(oracle, SRC).scalar(
            "count(vcc)"
        )
        await same_values(oracle, "circle")

    async def test_reload_passes_the_check(
        self, dags: PumpDags, oracle: OracleSide
    ) -> None:
        report = await _landed(dags, oracle, CIRCLE_RELOAD)

        assert f"{ROWS} rows loaded" in report
        assert "error" not in report.split("rows loaded")[0].lower()
        assert await OraLoaded(oracle, "circle").count() == ROWS


class TestOracleToOthers:
    async def test_postgres_creates_the_table(
        self, dags: PumpDags, oracle: OracleSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, oracle, TO_POSTGRES)
        landed = Loaded(postgres, PG_SCHEMA, from_table(oracle))
        source = OraLoaded(oracle, SRC)

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == EXPECTED_POSTGRES
        assert await landed.count() == ROWS
        assert await landed.aggregate("sum(n19)") == Decimal(
            str(await source.scalar("sum(n19)"))
        )
        assert await landed.aggregate("sum(n18_4)") == Decimal(
            str(await source.scalar("sum(n18_4)"))
        )
        assert await landed.scalars(
            "to_char(ts6, 'YYYY-MM-DD HH24:MI:SS.US')"
        ) == await source.column("to_char(ts6, 'yyyy-mm-dd hh24:mi:ss.ff6')")
        assert await landed.texts("vc") == await source.column("vc")
        assert await landed.texts("r16") == await source.column("rawtohex(r16)")

    async def test_clickhouse_creates_the_table(
        self, dags: PumpDags, oracle: OracleSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, oracle, TO_CLICKHOUSE)
        landed = ChLoaded(clickhouse, from_table(oracle))
        source = OraLoaded(oracle, SRC)

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == EXPECTED_CLICKHOUSE
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(n18_4)") == await source.scalar("sum(n18_4)")
        assert await landed.column("vc") == await source.column("vc")


PG_DDL = f"""
create table {PG_SCHEMA}.src (
    id bigint not null,
    i2 smallint,
    n numeric(12,2),
    f8 double precision,
    b boolean,
    t text,
    vc varchar(30),
    bin bytea,
    d date,
    ts timestamp(6),
    tz timestamptz(3),
    tm time,
    u uuid,
    j jsonb,
    ip inet,
    iv interval,
    arr integer[]
)"""
PG_FILL = f"""
insert into {PG_SCHEMA}.src
select g, (g % 30000)::smallint, g * 1.25, g / 7.0, g % 2 = 0,
       'text ' || g || E' \\t tab', 'v' || g, decode(lpad(to_hex(g), 6, '0'), 'hex'),
       date '2024-01-01' + g % 300,
       timestamp '2024-02-29 13:14:15.123456' + (g || ' seconds')::interval,
       timestamptz '2024-02-29 13:14:15.123+00' + (g || ' seconds')::interval,
       time '13:14:15.123456' + (g || ' seconds')::interval,
       md5(g::text)::uuid, jsonb_build_object('g', g), ('10.0.0.' || g % 250)::inet,
       (g || ' minutes')::interval, array[g, g + 1]
from generate_series(1, {ROWS}) g"""

EXPECTED_FROM_POSTGRES = [
    ("id", "NUMBER(19,0)", True),
    ("i2", "NUMBER(5,0)", False),
    ("n", "NUMBER(12,2)", False),
    ("f8", "BINARY_DOUBLE", False),
    ("b", "{boolean}", False),
    ("t", "CLOB", False),
    ("vc", "VARCHAR2(30 CHAR)", False),
    ("bin", "CLOB", False),
    ("d", "DATE", False),
    ("ts", "TIMESTAMP(6)", False),
    ("tz", "TIMESTAMP(3) WITH TIME ZONE", False),
    ("tm", "VARCHAR2(18 CHAR)", False),
    ("u", "VARCHAR2(36 CHAR)", False),
    ("j", "CLOB", False),
    ("ip", "CLOB", False),
    ("iv", "CLOB", False),
]
"""Таблица Oracle из потока postgres без массива: bytea postgres присылает
hex-текстом, и он ложится строкой как есть; boolean до 23 источник шлёт
числом (b::int), это integer — NUMBER(10)."""


def expected_from_postgres(oracle: OracleSide) -> list[tuple[str, str, bool]]:
    boolean = "BOOLEAN"
    if oracle.version < BOOLEAN_SINCE:
        boolean = "NUMBER(10,0)"

    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_FROM_POSTGRES:
        columns.append((name, kind.format(boolean=boolean), not_null))

    return columns


@pytest.fixture(scope="module")
async def pg_source(postgres: PostgresSide) -> PostgresSide:
    await postgres.execute([PG_DDL, PG_FILL])

    return postgres


class TestPostgresIntoOracle:
    @WITHOUT_BOOLEAN
    async def test_raw_bool_is_refused_before_23(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        assert oracle.version < BOOLEAN_SINCE, oracle.source.name

        failure = await _refused(dags, oracle, PG_RAW_BOOL, "dst")

        assert re.search(r"column b: bool .*send an integer 0 or 1", failure.llm_view())

    async def test_array_is_refused(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        failure = await _refused(dags, oracle, PG_ARRAY_REFUSED, "dst")

        assert re.search(r"column arr: .*integer\[\]", failure.llm_view())

    async def test_array_lands_as_clob_on_fallback(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        report = await _landed(dags, oracle, PG_ARRAY_AS_CLOB)
        landed = OraLoaded(oracle, "from_pg")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == [
            ("id", "NUMBER(19,0)", True),
            ("arr", "CLOB", False),
        ]
        assert (await landed.column("to_char(arr)"))[:2] == ["{1,2}", "{2,3}"]

    async def test_every_family_lands_with_oracle_types(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        report = await _landed(dags, oracle, PG_EVERY_FAMILY)
        landed = OraLoaded(oracle, "from_pg")
        source = Loaded(pg_source, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == expected_from_postgres(oracle)
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(n)") == await source.aggregate("sum(n)")
        assert await landed.column("u") == await source.texts("u")
        assert await landed.column("tm") == await source.texts("tm")
        assert await landed.column("to_char(d, 'yyyy-mm-dd')") == (
            await source.texts("d")
        )
        assert await landed.column(
            "to_char(ts, 'yyyy-mm-dd hh24:mi:ss.ff6')"
        ) == await source.scalars("to_char(ts, 'YYYY-MM-DD HH24:MI:SS.US')")
        assert await landed.column("to_char(cast(j as varchar2(100)))") == (
            await source.texts("j")
        )
        assert await landed.column("to_char(bin)") == await source.texts("bin")
        truthy_expression = "count(case when b = true then 1 end)"
        if oracle.version < BOOLEAN_SINCE:
            truthy_expression = "sum(b)"

        truthy = await landed.scalar(truthy_expression)

        assert truthy == await source.aggregate("count(*) filter (where b)")

    async def test_reload_passes_the_check(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        report = await _landed(dags, oracle, PG_RELOAD)

        assert f"{ROWS} rows loaded" in report
        assert "error" not in report.split("rows loaded")[0].lower()

    async def test_zoned_stream_into_unzoned_table_is_refused(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        await _landed(dags, oracle, PG_MOMENTS)

        failure = await _refused(dags, oracle, PG_MOMENTS_ZONED, "dst")

        assert "ts: time zone differs" in failure.llm_view()


class TestStrategies:
    """Стратегии приёмника на потоке postgres -> Oracle."""

    async def test_delete_where_replaces_a_slice(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        await _landed(dags, oracle, SLICES)
        landed = OraLoaded(oracle, "slices")
        await oracle.run(
            (
                f"update {OWNER}.slices set vc = 'stale' "
                f"where to_char(d, 'yyyy-mm') = '{MONTH}'",
            )
        )
        in_month = await Loaded(pg_source, PG_SCHEMA, "src").aggregate(
            f"count(*) filter (where to_char(d, 'YYYY-MM') = '{MONTH}')"
        )

        report = await _landed(dags, oracle, SLICES_MONTH)

        assert in_month > 0
        assert f"{in_month} rows deleted" in report
        assert f"{in_month} rows loaded" in report
        assert await landed.scalar("count(case when vc = 'stale' then 1 end)") == 0
        assert await landed.count() == ROWS

    async def test_renamed_mart_with_declared_types(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        report = await _landed(dags, oracle, MART)
        landed = OraLoaded(oracle, "mart")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == [
            ("key", "NUMBER(19,0)", True),
            ("amount", "NUMBER(14,3)", False),
            ("label", "VARCHAR2(10)", False),
            ("ts", "TIMESTAMP(6)", False),
        ]
        assert await landed.column("label", order_by="key") == await Loaded(
            pg_source, PG_SCHEMA, "src"
        ).texts("vc")

    async def test_narrower_table_is_refused_before_loading(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        await oracle.create(
            "narrow", ["id number(19) not null", "n number(8,1)", "vc varchar2(3)"]
        )
        try:
            failure = await _refused(dags, oracle, NARROW, "dst")

            assert re.search(r"n: table NUMBER\(8,1\) truncates", failure.llm_view())
            assert await OraLoaded(oracle, "narrow").count() == 0
        finally:
            await oracle.drop_table("narrow")

    async def test_drift_is_refused_then_backed_up(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        await _landed(dags, oracle, DRIFT)
        await oracle.run((f"alter table {OWNER}.drift drop column vc",))

        failure = await _refused(dags, oracle, DRIFT_CHECKED, "dst")

        assert "vc: in the stream but not in the table" in failure.llm_view()

        report = await _landed(dags, oracle, DRIFT_BACKED_UP)
        drift = OraLoaded(oracle, "drift")
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "drift_bak_" in report
        assert len(backups) == 1
        assert await OraLoaded(oracle, backups[0]).count() == ROWS
        assert await drift.count() == ROWS
        assert ("vc", "VARCHAR2(30 CHAR)", False) in await drift.columns()

    async def test_create_template_and_after_steps(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        report = await _landed(dags, oracle, TEMPLATED)
        landed = OraLoaded(oracle, "templated")

        assert f"{ROWS} rows loaded" in report
        assert f"statement: update {OWNER}.templated set n = n * 2" in report
        assert await landed.scalar("sum(n)") == 2 * await Loaded(
            pg_source, PG_SCHEMA, "src"
        ).aggregate("sum(n)")
        assert (
            await landed.scalar(
                f"(select logging from all_tables where owner = '{OWNER}' "
                "and table_name = 'TEMPLATED')"
            )
            == "NO"
        )

    async def test_dry_run_changes_nothing(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        landed = OraLoaded(oracle, "templated")
        columns = await landed.columns()

        report = await _landed(dags, oracle, DRY_RUN)

        assert report.startswith("0 rows loaded")
        assert await landed.columns() == columns
        assert await landed.count() == ROWS

    async def test_failed_after_step_rolls_back_the_rows(
        self, dags: PumpDags, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        landed = OraLoaded(oracle, "templated")

        failure = await _refused(dags, oracle, FAILED_AFTER_STEP, "dst")

        assert isinstance(failure, SqlFailureResult), failure
        assert "ORA-00942" in failure.llm_view()
        assert failure.statements[-1].status == "failed: OracleQueryError"
        # truncate — DDL с автокоммитом, а вставка откатилась вместе с ошибкой шага
        assert await landed.count() == 0


CH_DDL = f"""
create table {CH_DATABASE}.src (
    id Int64,
    u8 UInt8,
    dec Decimal(18, 4),
    f32 Float32,
    s String,
    lc LowCardinality(String),
    d Date,
    dt DateTime64(3, 'UTC'),
    b Bool,
    u UUID
) engine = MergeTree order by id"""
CH_FILL = f"""
insert into {CH_DATABASE}.src
select number, number % 200, toDecimal64(number, 4) / 7, number / 3,
       concat('s', toString(number)), concat('lc', toString(number % 3)),
       toDate('2024-01-01') + number % 300,
       toDateTime64('2024-02-29 13:14:15.123', 3, 'UTC') + number,
       number % 2 = 0, generateUUIDv4()
from numbers(1, {ROWS})"""

EXPECTED_FROM_CLICKHOUSE = [
    ("id", "NUMBER(19,0)", True),
    ("u8", "NUMBER(3,0)", True),
    ("dec", "NUMBER(18,4)", True),
    ("f32", "BINARY_FLOAT", True),
    ("s", "CLOB", False),
    ("lc", "CLOB", False),
    ("d", "DATE", True),
    ("dt", "TIMESTAMP(3) WITH TIME ZONE", True),
    ("b", "{boolean}", True),
    ("u", "CLOB", False),
]
"""Таблица Oracle из потока ClickHouse: uuid LLM шлёт текстом (toString),
bool до 23 — числом (toUInt8, это UInt8 — NUMBER(3))."""


@pytest.fixture(scope="module")
async def ch_source(clickhouse: ClickHouseSide) -> ClickHouseSide:
    await clickhouse.command(CH_DDL)
    await clickhouse.command(CH_FILL)

    return clickhouse


class TestClickHouseIntoOracle:
    async def test_raw_uuid_is_refused(
        self, dags: PumpDags, oracle: OracleSide, ch_source: ClickHouseSide
    ) -> None:
        failure = await _refused(dags, oracle, CH_RAW_UUID, "dst")

        assert re.search(
            r"column u: .*uuid extension: send utf8 text", failure.llm_view()
        )

    async def test_every_family_lands_with_oracle_types(
        self, dags: PumpDags, oracle: OracleSide, ch_source: ClickHouseSide
    ) -> None:
        report = await _landed(dags, oracle, CH_EVERY_FAMILY)
        landed = OraLoaded(oracle, "from_ch")
        source = ChLoaded(ch_source, "src")
        boolean = "BOOLEAN"
        if oracle.version < BOOLEAN_SINCE:
            boolean = "NUMBER(3,0)"

        expected: list[tuple[str, str, bool]] = []
        for name, kind, not_null in EXPECTED_FROM_CLICKHOUSE:
            expected.append((name, kind.format(boolean=boolean), not_null))

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == expected
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(dec)") == await source.scalar("sum(dec)")
        assert await landed.column("to_char(u)") == [
            str(value) for value in await source.column("u")
        ]
        assert await landed.column("to_char(s)") == await source.column("s")
        assert await landed.column(
            "to_char(sys_extract_utc(dt), 'yyyy-mm-dd hh24:mi:ss.ff3')"
        ) == await source.column("toString(dt)")


class TestSourceRefusals:
    """Типы, которые драйвер в Arrow отдаёт с потерей или не отдаёт: отказ
    до выполнения запроса с подсказкой."""

    UNFETCHABLE: ClassVar[list[tuple[str, str]]] = [
        (INTERVAL_DS, "INTERVAL_DS"),
        (INTERVAL_YM, "INTERVAL_YM"),
        (XMLTYPE, "xmlserialize"),
    ]

    async def test_zoned_timestamp_needs_a_conversion(
        self, dags: PumpDags, oracle: OracleSide
    ) -> None:
        failure = await _refused(dags, oracle, ZONED_TIMESTAMP, "src")

        assert failure.error_kind == "OracleQueryError"
        assert re.search(r"tstz.*sys_extract_utc", failure.llm_view())
        assert "refused" not in await OraLoaded(oracle, SRC).tables()

    async def test_utc_wall_time_lands_as_zoned_column(
        self, dags: PumpDags, oracle: OracleSide
    ) -> None:
        report = await _landed(dags, oracle, UTC_WALL_TIME)
        landed = OraLoaded(oracle, "utc")

        assert f"{ROWS} rows loaded" in report
        assert ("utc", "TIMESTAMP(6) WITH TIME ZONE", False) in await landed.columns()
        assert (
            await landed.column("to_char(utc, 'yyyy-mm-dd hh24:mi:ss.ff6 tzh:tzm')")
        )[0] == "2024-02-29 10:14:16.123456 +00:00"

    @pytest.mark.parametrize(
        ("spec", "hint"), UNFETCHABLE, ids=["interval_ds", "interval_ym", "xmltype"]
    )
    async def test_unfetchable_types_are_refused(
        self, dags: PumpDags, oracle: OracleSide, spec: str, hint: str
    ) -> None:
        failure = await _refused(dags, oracle, spec, "src")

        assert failure.error_kind == "OracleQueryError"
        assert hint in failure.llm_view()

    async def test_declaration_on_a_missing_column(
        self, dags: PumpDags, oracle: OracleSide
    ) -> None:
        failure = await _refused(dags, oracle, MISSING_DECLARATION, "src")

        assert failure.error_kind == "ContractError"
        assert "no column 'nope'" in failure.llm_view()
