# ruff: noqa: S608, E501
"""Пограничные случаи приёмника pg_stream_in: NULL и not null, decimal с
точностью и NaN, varchar с юникодом и длиной, timestamp с точностью, поясом
и бесконечностью, геометрия, диапазоны, json, массивы, пользовательские
типы; кросс-движковые потоки из ClickHouse (беззнаковые целые, наносекунды)
и Oracle (NUMBER без точности, NaN у BINARY_DOUBLE, CLOB в мегабайты).
Постгресы — по одному на поколение плюс Greenplum 6 и 7.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.
Подстановкой в описания идут имена стенда ($s, $ext, $ora_user) и
параметры матрицы: запись float $exact_floats (Greenplum 6), имя случая
$case и провод $wire у параметризованных тестов.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from boba.pump_stand import (
    ClickHouseSide,
    Loaded,
    OracleSide,
    PostgresSide,
    PumpDags,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.pump_stand.stand import PgSource
from boba.stand.ix import IxStand
from boba.stand.names import StandNames
from boba.toolkit.transfer import StreamWire
from boba.toolrun.dag_run import DagOutcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
S = StandNames().of("pump_edges")
EXT = PostgresSide.EXTENSION_SCHEMA
ROWS = 7
NULL_ROW = 5
TARGETS = ("pg-9.0", "pg-12", "pg-19", "gp-6", "gp-7")

ROUND_TRIP = """
name = "pg_edges_round_trip"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_$case"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_$case"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""
"""Таблицы случая матрицы: s_<случай> -> t_<случай>."""

NUMERIC_NAN = """
name = "pg_edges_numeric_nan"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_nan"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_nan"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NUMERIC_NAN_AS_FLOAT8 = """
name = "pg_edges_numeric_nan_as_float8"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, v::float8 as v from $s.s_nan"
wire = "arrow"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_nan_f8"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIMESTAMP_INFINITY = """
name = "pg_edges_timestamp_infinity"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_inf"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_inf"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIMESTAMP_INFINITY_AS_TEXT = """
name = "pg_edges_timestamp_infinity_as_text"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, v::text as v from $s.s_inf"
wire = "arrow"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_inf_text"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NUMERIC_WITHOUT_PRECISION = """
name = "pg_edges_numeric_without_precision"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_free"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_free"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NUMERIC_WIDER_THAN_DECIMAL128 = """
name = "pg_edges_numeric_wider_than_decimal128"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_wide"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_wide"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NULLS_AND_EMPTY_STRINGS = """
name = "pg_edges_nulls_and_empty_strings"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_nul"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_nul"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NOT_NULL_DECLARED = """
name = "pg_edges_not_null_declared"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_strict"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "v", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_strict"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NULLABLE_INTO_NOT_NULL = """
name = "pg_edges_nullable_into_not_null"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, v from $s.s_strict"
wire = "arrow"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_strict"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NULL_VALUE_INTO_NOT_NULL = """
name = "pg_edges_null_value_into_not_null"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, case when g = 5 then null else g end as v from generate_series(1, 7) g"
wire = "arrow"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_rollback"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

DECIMAL_EXACT = """
name = "pg_edges_decimal_exact"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_dec"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_dec"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

DECIMAL_WIDENED = """
name = "pg_edges_decimal_widened"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_dec_w"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_dec_w"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { v = "numeric(20,6)" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

DECIMAL_NARROWER_TARGET = """
name = "pg_edges_decimal_narrower_target"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_dec_n"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_dec_n"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

DECIMAL_WIDER_TARGET = """
name = "pg_edges_decimal_wider_target"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_dec_ok"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_dec_ok"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

VARCHAR_UNICODE = """
name = "pg_edges_varchar_unicode"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_uni"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_uni"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

VARCHAR_SHORTER_COLUMN = """
name = "pg_edges_varchar_shorter_column"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_v100"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_v50"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

VARCHAR_SHORTER_COLUMN_RECREATED = """
name = "pg_edges_varchar_shorter_column_recreated"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_v100"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_v50"
schema_strategy = { kind = "drop_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TEXT_INTO_LIMITED_COLUMN = """
name = "pg_edges_text_into_limited_column"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_text"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_lim"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

LONG_TEXT_INTO_LIMITED_COLUMN = """
name = "pg_edges_long_text_into_limited_column"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_text_long"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_lim"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

CHAR_PADDING = """
name = "pg_edges_char_padding"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_ch"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_ch"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIMESTAMP_MILLISECONDS = """
name = "pg_edges_timestamp_milliseconds"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_ms"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_ms"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIMESTAMP_FINER_STREAM = """
name = "pg_edges_timestamp_finer_stream"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_us"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_s0"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIMESTAMP_COARSER_STREAM = """
name = "pg_edges_timestamp_coarser_stream"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_s0"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_us"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIME_ZONE_MISMATCH = """
name = "pg_edges_time_zone_mismatch"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_tz"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_naive"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TIMESTAMPTZ_OVER_DST = """
name = "pg_edges_timestamptz_over_dst"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_dst"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_dst"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

DATE_INTO_TIMESTAMP = """
name = "pg_edges_date_into_timestamp"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_date"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_ts_for_date"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

DOUBLE_BITS = """
name = "pg_edges_double_bits"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_bits"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_bits"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

CH_UNSIGNED_AND_NANOSECONDS = """
name = "pg_edges_ch_unsigned_and_nanoseconds"
version = 1

[[nodes]]
key = "out"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $s.edges order by id settings output_format_arrow_string_as_string = 1"
wire = "arrow"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "from_ch"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

CH_UNSIGNED_INTO_SIGNED = """
name = "pg_edges_ch_unsigned_into_signed"
version = 1

[[nodes]]
key = "out"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $s.u64only"
wire = "arrow"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_u64"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

ORA_EDGES = """
name = "pg_edges_ora_edges"
version = 1

[[nodes]]
key = "out"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = "select * from $ora_user.edges"
columns = [{ name = "ID", nullable = false }]
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "from_ora"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

COPY_VERBATIM = """
name = "pg_edges_copy_verbatim"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_$case"
wire = "csv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_$case"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""
"""Таблицы случая матрицы: s_<случай> -> t_<случай>."""

COPY_ENUM_AS_VARCHAR = """
name = "pg_edges_copy_enum_as_varchar"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_copy_en"
wire = "csv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_copy_en"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

COPY_ENUM_TYPED = """
name = "pg_edges_copy_enum_typed"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_copy_en"
wire = "csv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_copy_en_typed"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { v = "$s.mood" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

COPY_NARROWER_TARGET = """
name = "pg_edges_copy_narrower_target"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_copy_bk"
wire = "csv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_copy_bk"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

COPY_NARROWER_TARGET_RECREATED = """
name = "pg_edges_copy_narrower_target_recreated"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_copy_bk"
wire = "csv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_copy_bk"
schema_strategy = { kind = "drop_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

EXTENSION_AS_VARCHAR = """
name = "pg_edges_extension_as_varchar"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_${case}_$wire"
wire = "$wire"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_${case}_$wire"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""
"""Таблицы случая и провода матрицы: s_<случай>_<провод>."""

EXTENSION_TYPED = """
name = "pg_edges_extension_typed"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_${case}_$wire"
wire = "$wire"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_${case}_${wire}_typed"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { v = "$ext.$case" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""
"""Тип колонки — тип расширения случая: его имя совпадает с именем случая."""

HSTORE_UNDECLARED = """
name = "pg_edges_hstore_undeclared"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_hs_asis"
wire = "csv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_hs_asis"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

HSTORE_DECLARED_AT_SOURCE = """
name = "pg_edges_hstore_declared_at_source"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_hs_asis"
wire = "csv"
columns = [{ name = "id", nullable = false }, { name = "v", type_text = "$ext.hstore" }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_hs_asis"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

COLUMN_TYPES_FOR_MISSING_COLUMN = """
name = "pg_edges_column_types_for_missing_column"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_ct_miss"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_ct_miss"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { nope = "text" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

VECTOR_TYPED = """
name = "pg_edges_vector_typed"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_vec_$wire"
wire = "$wire"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_vec_$wire"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { v = "vector(3)", h = "halfvec(2)" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

VECTOR_AS_VARCHAR = """
name = "pg_edges_vector_as_varchar"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_vec_ddl"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_vec_ddl"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

BACKUP_AND_CREATE = """
name = "pg_edges_backup_and_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_bk"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_bk"
schema_strategy = { kind = "backup_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""


def _chosen(sources: Sequence[Any]) -> list[Any]:
    chosen: list[Any] = []
    for source in sources:
        if source.name in TARGETS:
            chosen.append(source)

    return chosen


NEWEST = STAND.newest_postgres()
ON_NEWEST = STAND.only("postgres", [NEWEST])
"""Случай не зависит от версии сервера: идёт на одном, самом новом postgres."""

EXTENSIONS_SINCE = 90100
"""create extension появился в 9.1."""


def _since(kernel: int) -> pytest.MarkDecorator:
    """Серверы набора с ядром не старше данного."""
    chosen: list[PgSource] = []
    for source in _chosen(STAND.sources):
        if source.kernel >= kernel:
            chosen.append(source)

    return STAND.only("postgres", chosen)


@pytest.fixture(scope="module", params=_chosen(STAND.sources), ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, S)
    await side.connect()
    await side.recreate_schema(
        [
            "create type mood as enum ('sad', 'happy')",
            "create type pair as (a int, b text)",
            "create domain posint as int check (value > 0)",
        ]
    )
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(STAND.demo_clickhouse()[-1], S)
    await side.connect()
    await side.recreate_database()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def oracle() -> AsyncIterator[OracleSide]:
    side = OracleSide(max(STAND.ora_sources, key=lambda s: s.name), 500)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def vector_db() -> AsyncIterator[PostgresSide]:
    """Сервер ix стенда с pgvector: стендовые контейнеры насосов расширения
    vector не имеют. Берётся служебная база сервера, а не базы прогонов ix —
    их пересоздают наборы ix; своя схема процесса изолирует данные."""
    side = PostgresSide(PgSource(name="ix", postgres=IxStand.required().postgres), S)
    await side.connect()
    await side.ensure_extensions(["vector"])
    await side.recreate_schema()
    yield side
    await side.drop()


def toml_bool(value: bool) -> str:
    if value:
        return "true"

    return "false"


def stand_names(postgres: PostgresSide) -> dict[str, str]:
    """Имена стенда и особенность сервера для описаний postgres -> postgres."""
    return {"s": S, "ext": EXT, "exact_floats": toml_bool(postgres.greenplum_6)}


@pytest.fixture
def dags(tmp_path: Path, postgres: PostgresSide) -> PumpDags:
    """Запуск описаний источник -> приёмник в одной схеме сервера."""
    return PumpDags(tmp_path, {"pg": postgres.profile}, stand_names(postgres))


@pytest.fixture
def case_dags(tmp_path: Path, postgres: PostgresSide, case: Case) -> PumpDags:
    """То же для тестов, параметризованных случаем: его имя — $case."""
    names = stand_names(postgres)
    names["case"] = case.name

    return PumpDags(tmp_path, {"pg": postgres.profile}, names)


@pytest.fixture
def case_wire_dags(
    tmp_path: Path, postgres: PostgresSide, case: Case, mode: StreamWire
) -> PumpDags:
    """То же для тестов по случаю и проводу: $case и $wire."""
    names = stand_names(postgres)
    names["case"] = case.name
    names["wire"] = mode.value

    return PumpDags(tmp_path, {"pg": postgres.profile}, names)


@pytest.fixture
def vector_dags(tmp_path: Path, vector_db: PostgresSide) -> PumpDags:
    """Запуск описаний на сервере с pgvector."""
    return PumpDags(tmp_path, {"pg": vector_db.profile}, stand_names(vector_db))


@pytest.fixture
def vector_wire_dags(
    tmp_path: Path, vector_db: PostgresSide, mode: StreamWire
) -> PumpDags:
    """То же для теста, параметризованного проводом: $wire."""
    names = stand_names(vector_db)
    names["wire"] = mode.value

    return PumpDags(tmp_path, {"pg": vector_db.profile}, names)


@pytest.fixture
def ch_dags(
    tmp_path: Path, postgres: PostgresSide, clickhouse: ClickHouseSide
) -> PumpDags:
    """Запуск описаний ClickHouse -> postgres."""
    return PumpDags(
        tmp_path, {"pg": postgres.profile, "ch": clickhouse.profile}, {"s": S}
    )


@pytest.fixture
def ora_dags(tmp_path: Path, postgres: PostgresSide, oracle: OracleSide) -> PumpDags:
    """Запуск описаний Oracle -> postgres."""
    return PumpDags(
        tmp_path,
        {"pg": postgres.profile, "ora": oracle.profile},
        {"s": S, "ora_user": PumpUser().name},
    )


def landed(outcome: DagOutcome) -> str:
    """Отчёт приёмника удавшегося запуска."""
    report = outcome.node("in").content
    print(f"\n--- {outcome.dag}: pg_stream_in ---\n{report}")

    assert outcome.ok(), outcome.node("out").content

    return report


def refused(outcome: DagOutcome, key: str, error_kind: str) -> str:
    """Текст отказа узла key, исходная ошибка которого — класса error_kind."""
    failure = outcome.failure(key)
    text = failure.llm_view()
    print(f"\n--- {outcome.dag}: {key} failed ---\n{text}")

    assert failure.error_kind == error_kind, text

    return text


async def fill(postgres: PostgresSide, table: str, kind: str, expr: str) -> None:
    """Таблица id + одна колонка kind, ROWS строк по выражению над g, строка
    NULL_ROW — NULL."""
    await postgres.execute(
        [
            f"drop table if exists {S}.{table}",
            f"create table {S}.{table} (id bigint not null, v {kind})",
            f"insert into {S}.{table} select g, case when g = {NULL_ROW} "
            f"then null else ({expr}) end from generate_series(1, {ROWS}) g",
        ]
    )


@dataclass(frozen=True)
class Case:
    """Тип источника: DDL, заполнение над g, минимальная версия сервера и
    тип, который ждём у приёмника (пусто — тот же, что у источника)."""

    name: str
    kind: str
    expr: str
    min_version: int = 0
    lands_as: str = ""


def _applicable(cases: Sequence[Case]) -> pytest.MarkDecorator:
    """Пары сервер — случай, где ядро сервера не старше минимальной версии
    случая: неприменимые сочетания не порождаются."""
    pairs: list[Any] = []
    for source in _chosen(STAND.sources):
        for case in cases:
            if source.kernel < case.min_version:
                continue

            pairs.append(pytest.param(source, case, id=f"{source.name}-{case.name}"))

    return pytest.mark.parametrize(("postgres", "case"), pairs, indirect=["postgres"])


CASES = [
    Case("ts_bc", "timestamp", "timestamp '0001-01-01 00:00:00'"),
    Case(
        "ts_max",
        "timestamp(6)",
        "timestamp '9999-12-31 23:59:59.999999' - g * interval '1 us'",
    ),
    Case(
        "ts_ms",
        "timestamp(3)",
        "timestamp '2024-02-29 13:14:15.123' + g * interval '1 ms'",
    ),
    Case(
        "tstz",
        "timestamptz",
        "timestamptz '2024-03-31 02:30:00 Europe/Moscow' + g * interval '1 hour'",
    ),
    Case("t6", "time(6)", "time '23:59:59.999999' - g * interval '1 us'"),
    Case("ttz", "timetz", "timetz '12:00:00+05:30'"),
    Case("iv", "interval", "interval '1 year 2 mons 3 days 04:05:06.789' * g"),
    Case("d", "date", "date '0001-01-01' + g"),
    Case("pt", "point", "point(g * 1.5, -2.25)"),
    Case("bx", "box", "box(point(0,0), point(g,1))"),
    Case("pl", "polygon", "polygon '((0,0),(1,0),(1,1))'"),
    Case("ci", "circle", "circle(point(0,0), g)"),
    Case("ln", "line", "line '{1,2,3}'", 90400),
    Case(
        "js",
        "jsonb",
        "jsonb_build_object('a', g, 'b', array[1,2], 'c', null, 'd', 'x\"y\\n')",
        90500,
    ),
    Case("jsn", "json", '\'{"k": "v"}\'::json', 90200),
    Case("u", "uuid", "'a1b2c3d4-0000-0000-0000-000000000001'::uuid"),
    Case("ip", "inet", "'10.0.0.1/24'::inet"),
    Case("cidr", "cidr", "'10.0.0.0/8'::cidr"),
    Case("mac", "macaddr", "'08:00:2b:01:02:03'::macaddr"),
    Case("by", "bytea", "decode('00ff10' || lpad(g::text, 2, '0'), 'hex')"),
    Case("arr", "int[]", "array[g, null, 3]"),
    Case(
        "arr2",
        "text[]",
        "array['a b', 'q\"uote', 'back\\slash', 'comma,', '', 'NULL', null]",
    ),
    Case("arr2d", "int[][]", "array[array[1,2],array[3,4]]", 0, "integer[]"),
    Case("narr", "numeric(5,2)[]", "array[1.25, 2.5]"),
    Case("en", "mood", "'happy'::mood", 0, "character varying"),
    Case("comp", "pair", "row(g, 'x')::pair", 0, "character varying"),
    Case("rng", "int4range", "int4range(1, g + 1)", 90200),
    Case("tsr", "tstzrange", "tstzrange('2024-01-01', '2024-02-01')", 90200),
    Case("mon", "money", "'12.5'::money * g"),
    Case("bt", "bit(8)", "B'10101010'"),
    Case("vb", "varbit", "B'101'"),
    Case("xm", "xml", "'<a b=\"1\">x</a>'::xml"),
    Case("tsv", "tsvector", "to_tsvector('english', 'the quick brown fox')"),
    Case("ch5", "char(5)", "'ab'"),
    Case("vcu", "varchar(5)", "'😀😀😀😀😀'"),
    Case(
        "txt",
        "text",
        "case g when 1 then '' when 2 then E'\\\\N' when 3 then E'a\\tb\\nc' "
        "else E'q''\"\\\\' end",
    ),
    Case("dom", "posint", "g", 0, "integer"),
    Case(
        "f8",
        "float8",
        "case g when 1 then 'NaN'::float8 when 2 then 'inf' when 3 then '-inf' "
        "else g / 3.0 end",
    ),
    Case("f4", "real", "(g / 3.0)::real"),
    Case("big", "numeric(38,10)", "9999999999999999999999999999.9999999999 - g"),
    Case("neg", "numeric(18,4)", "-g / 7.0"),
    Case("i2", "smallint", "-32768 + g"),
    Case("b", "boolean", "g % 2 = 0"),
]


class TestRoundTrip:
    """Каждый тип: таблица-источник с NULL в одной строке, pg_stream_out ->
    pg_stream_in создаёт приёмник, значения текстом равны, тип приёмника
    ожидаемый."""

    @_applicable(CASES)
    async def test_type_lands_as_itself(
        self, case_dags: PumpDags, postgres: PostgresSide, case: Case
    ) -> None:
        assert postgres.version >= case.min_version, postgres.source.name

        await fill(postgres, f"s_{case.name}", case.kind, case.expr)
        report = landed(await case_dags.run(ROUND_TRIP))
        source = Loaded(postgres, S, f"s_{case.name}")
        target = Loaded(postgres, S, f"t_{case.name}")
        source_type = (await source.columns())[1][1]
        expected = case.lands_as
        if not expected:
            expected = source_type

        assert report.startswith(f"{ROWS} rows loaded")
        assert (await target.columns())[1] == ("v", expected, False)
        assert await target.texts("v") == await source.texts("v")


class TestRefusedValues:
    """Значения, которых читатель CSV Arrow не собирает: отказ с причиной."""

    @ON_NEWEST
    async def test_numeric_nan_needs_a_cast(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_nan",
            "numeric(10,2)",
            "case g when 1 then 'NaN'::numeric else g end",
        )
        outcome = await dags.run(NUMERIC_NAN)

        assert "NaN" in refused(outcome, "out", "PgArrowError")

        report = landed(await dags.run(NUMERIC_NAN_AS_FLOAT8))

        assert report.startswith(f"{ROWS} rows loaded")
        assert (await Loaded(postgres, S, "t_nan_f8").texts("v"))[0] == "NaN"

    @ON_NEWEST
    async def test_timestamp_infinity_needs_a_cast(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_inf",
            "timestamp",
            "case g when 1 then 'infinity'::timestamp when 2 then '-infinity' "
            "else timestamp '2024-01-01' end",
        )
        outcome = await dags.run(TIMESTAMP_INFINITY)

        assert "infinity" in refused(outcome, "out", "PgArrowError")

        report = landed(await dags.run(TIMESTAMP_INFINITY_AS_TEXT))

        assert report.startswith(f"{ROWS} rows loaded")
        assert (await Loaded(postgres, S, "t_inf_text").texts("v"))[:2] == [
            "infinity",
            "-infinity",
        ]

    @ON_NEWEST
    async def test_numeric_without_precision_is_refused_before_execution(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_free", "numeric", "g / 7.0")
        outcome = await dags.run(NUMERIC_WITHOUT_PRECISION)

        assert "numeric without precision" in refused(outcome, "out", "PgArrowError")

    @ON_NEWEST
    async def test_numeric_wider_than_decimal128_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_wide", "numeric(40,2)", "g")
        outcome = await dags.run(NUMERIC_WIDER_THAN_DECIMAL128)

        assert "up to 38 digits" in refused(outcome, "out", "PgArrowError")


class TestNulls:
    async def test_nulls_and_empty_strings_are_distinct(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_nul",
            "text",
            "case g when 1 then '' when 2 then E'\\\\N' else 'x' end",
        )
        landed(await dags.run(NULLS_AND_EMPTY_STRINGS))
        values = await Loaded(postgres, S, "t_nul").texts("v")

        assert values[0] == ""
        assert values[1] == "\\N"
        assert values[NULL_ROW - 1] is None
        assert values.count(None) == 1

    async def test_not_null_travels_and_a_nullable_stream_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.s_strict",
                f"create table {S}.s_strict (id bigint not null, v int not null)",
                f"insert into {S}.s_strict select g, g "
                f"from generate_series(1, {ROWS}) g",
            ]
        )
        landed(await dags.run(NOT_NULL_DECLARED))

        assert (await Loaded(postgres, S, "t_strict").columns())[1] == (
            "v",
            "integer",
            True,
        )

        outcome = await dags.run(NULLABLE_INTO_NOT_NULL)

        assert "stream is nullable, table column is not null" in refused(
            outcome, "in", "TransferError"
        )

    async def test_null_into_not_null_rolls_the_whole_load_back(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.t_rollback",
                f"create table {S}.t_rollback (id bigint not null, v int not null)",
                f"insert into {S}.t_rollback values (0, 0)",
            ]
        )
        outcome = await dags.run(NULL_VALUE_INTO_NOT_NULL)
        refused(outcome, "in", "NotNullViolation")

        assert await Loaded(postgres, S, "t_rollback").count() == 1


class TestDecimal:
    async def test_values_are_exact(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_dec",
            "numeric(18,4)",
            "case g when 1 then 0 when 2 then -0.0001 when 3 then 99999999999999.9999 "
            "when 4 then -99999999999999.9999 else g / 7.0 end",
        )
        landed(await dags.run(DECIMAL_EXACT))
        landed_values = await Loaded(postgres, S, "t_dec").scalars("v")

        assert landed_values[:4] == [
            Decimal("0.0000"),
            Decimal("-0.0001"),
            Decimal("99999999999999.9999"),
            Decimal("-99999999999999.9999"),
        ]
        assert landed_values[NULL_ROW - 1] is None

    async def test_declaration_widens_the_target_and_keeps_values(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_dec_w", "numeric(18,4)", "g / 7.0")
        landed(await dags.run(DECIMAL_WIDENED))
        loaded = Loaded(postgres, S, "t_dec_w")

        assert (await loaded.columns())[1] == ("v", "numeric(20,6)", False)
        assert (await loaded.scalars("v"))[0] == Decimal("0.142900")

    @pytest.mark.parametrize(
        ("target_type", "reason"),
        [
            ("numeric(10,2)", "truncates the scale"),
            ("numeric(18,2)", "truncates the scale"),
            ("numeric(15,4)", "fewer integer digits"),
            ("bigint", "type family differs"),
            ("double precision", "type family differs"),
        ],
    )
    async def test_narrower_target_is_a_schema_change(
        self, dags: PumpDags, postgres: PostgresSide, target_type: str, reason: str
    ) -> None:
        await fill(postgres, "s_dec_n", "numeric(18,4)", "g / 7.0")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_dec_n",
                f"create table {S}.t_dec_n (id bigint not null, v {target_type})",
            ]
        )
        outcome = await dags.run(DECIMAL_NARROWER_TARGET)

        assert reason in refused(outcome, "in", "TransferError")

    async def test_wider_target_is_only_a_warning(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_dec_ok", "numeric(18,4)", "g / 7.0")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_dec_ok",
                f"create table {S}.t_dec_ok (id bigint not null, v numeric(20,6))",
            ]
        )
        report = landed(await dags.run(DECIMAL_WIDER_TARGET))

        assert "table numeric128" not in report
        assert '"column": "v"' in report
        assert await Loaded(postgres, S, "t_dec_ok").count() == ROWS


class TestVarchar:
    async def test_unicode_length_counts_characters(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_uni", "varchar(7)", "repeat('😀', g)")
        landed(await dags.run(VARCHAR_UNICODE))
        loaded = Loaded(postgres, S, "t_uni")

        assert (await loaded.columns())[1] == ("v", "character varying(7)", False)
        assert (await loaded.texts("v"))[6] == "😀" * 7

    async def test_longer_stream_into_shorter_column_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_v100", "varchar(100)", "repeat('x', g)")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_v50",
                f"create table {S}.t_v50 (id bigint not null, v varchar(50))",
            ]
        )
        outcome = await dags.run(VARCHAR_SHORTER_COLUMN)

        assert "table length 50 is shorter than stream length 100" in refused(
            outcome, "in", "TransferError"
        )

        report = landed(await dags.run(VARCHAR_SHORTER_COLUMN_RECREATED))

        assert "table: dropped and recreated" in report
        assert (await Loaded(postgres, S, "t_v50").columns())[1] == (
            "v",
            "character varying(100)",
            False,
        )

    async def test_text_into_limited_column_warns_and_the_server_checks_values(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_text", "text", "repeat('y', g)")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_lim",
                f"create table {S}.t_lim (id bigint not null, v varchar(10))",
            ]
        )
        report = landed(await dags.run(TEXT_INTO_LIMITED_COLUMN))

        assert re.search(
            r'"column": "v",[^}]*"note": "table limits the length to 10', report
        )

        await fill(postgres, "s_text_long", "text", "repeat('y', g * 10)")
        outcome = await dags.run(LONG_TEXT_INTO_LIMITED_COLUMN)
        refused(outcome, "in", "StringDataRightTruncation")

        assert await Loaded(postgres, S, "t_lim").count() == ROWS

    async def test_char_padding_survives(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_ch", "char(5)", "'a' || g")
        landed(await dags.run(CHAR_PADDING))

        assert (await Loaded(postgres, S, "t_ch").scalars("length(v)"))[0] == 2
        assert (await Loaded(postgres, S, "t_ch").scalars("octet_length(v)"))[0] == 5


class TestTimestamps:
    async def test_milliseconds_keep_their_precision(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_ms",
            "timestamp(3)",
            "timestamp '2024-02-29 13:14:15.123' + g * interval '1 ms'",
        )
        landed(await dags.run(TIMESTAMP_MILLISECONDS))
        loaded = Loaded(postgres, S, "t_ms")

        assert (await loaded.columns())[1] == (
            "v",
            "timestamp(3) without time zone",
            False,
        )
        assert (await loaded.texts("v"))[0] == "2024-02-29 13:14:15.124"

    async def test_finer_stream_into_coarser_column_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres, "s_us", "timestamp(6)", "timestamp '2024-02-29 13:14:15.123456'"
        )
        await postgres.execute(
            [
                f"drop table if exists {S}.t_s0",
                f"create table {S}.t_s0 (id bigint not null, v timestamp(0))",
            ]
        )
        outcome = await dags.run(TIMESTAMP_FINER_STREAM)

        assert "coarser" in refused(outcome, "in", "TransferError")

    async def test_coarser_stream_into_finer_column_only_warns(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_s0", "timestamp(0)", "timestamp '2024-02-29 13:14:15'")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_us",
                f"create table {S}.t_us (id bigint not null, v timestamp(6))",
            ]
        )
        report = landed(await dags.run(TIMESTAMP_COARSER_STREAM))

        assert re.search(
            r'"column": "v",[^}]*"note": "table [^"]+ is finer than', report
        )

    async def test_time_zone_mismatch_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres, "s_tz", "timestamptz", "timestamptz '2024-02-29 13:14:15+03'"
        )
        await postgres.execute(
            [
                f"drop table if exists {S}.t_naive",
                f"create table {S}.t_naive (id bigint not null, v timestamp)",
            ]
        )
        outcome = await dags.run(TIME_ZONE_MISMATCH)

        assert "time zone differs" in refused(outcome, "in", "TransferError")

    async def test_instant_survives_dst_and_session_zones(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_dst",
            "timestamptz",
            "timestamptz '2024-03-31 02:30:00 Europe/Moscow' + g * interval '1 hour'",
        )
        landed(await dags.run(TIMESTAMPTZ_OVER_DST))
        source = Loaded(postgres, S, "s_dst")
        target = Loaded(postgres, S, "t_dst")

        assert await target.scalars("extract(epoch from v)") == await source.scalars(
            "extract(epoch from v)"
        )

    async def test_date_into_timestamp_is_a_family_change(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_date", "date", "date '2024-02-29' + g")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_ts_for_date",
                f"create table {S}.t_ts_for_date (id bigint not null, v timestamp)",
            ]
        )
        outcome = await dags.run(DATE_INTO_TIMESTAMP)

        assert "type family differs" in refused(outcome, "in", "TransferError")


class TestFloats:
    async def test_doubles_land_bit_for_bit(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        """Включая 1.942e-297, которое Greenplum 6 в десятичной записи
        округляет: у него загрузка идёт hex-записью."""
        await fill(
            postgres,
            "s_bits",
            "float8",
            "case g when 1 then 1942::float8 * 1e-300 when 2 then "
            "2.2250738585072014e-308 "
            "when 3 then 1e308 when 4 then -0.0 else g / 3.0 end",
        )
        landed(await dags.run(DOUBLE_BITS))
        source = Loaded(postgres, S, "s_bits")
        target = Loaded(postgres, S, "t_bits")

        assert await target.scalars("float8send(v)::text") == await source.scalars(
            "float8send(v)::text"
        )


class TestClickHouseSources:
    @ON_NEWEST
    async def test_unsigned_and_nanoseconds_land(
        self, ch_dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.create(
            "edges",
            [
                "id Int64",
                "u64 UInt64",
                "u8 UInt8",
                "d DateTime64(9, 'UTC')",
                "s Nullable(String)",
                "dec Decimal(18, 4)",
            ],
        )
        await clickhouse.command(
            f"insert into {S}.edges select number, 18446744073709551615 - number, "
            "255 - toUInt8(number), "
            "toDateTime64('2024-02-29 13:14:15.123456789', 9, 'UTC'), "
            "if(number = 2, null, concat('s', toString(number))), "
            "toDecimal64(number / 7, 4) "
            "from numbers(5)"
        )
        report = landed(await ch_dags.run(CH_UNSIGNED_AND_NANOSECONDS))
        loaded = Loaded(postgres, S, "from_ch")
        columns = await loaded.columns()

        assert report.startswith("5 rows loaded")
        assert ("u64", "numeric(20,0)", True) in columns
        assert ("u8", "smallint", True) in columns
        assert ("d", "timestamp(6) with time zone", True) in columns
        assert ("s", "text", False) in columns
        assert ("dec", "numeric(18,4)", True) in columns
        assert (await loaded.scalars("u64"))[0] == Decimal("18446744073709551615")
        assert (await loaded.texts("d"))[0] == "2024-02-29 13:14:15.123457+00"
        assert (await loaded.texts("s"))[2] is None

    @ON_NEWEST
    async def test_unsigned_into_signed_column_is_refused(
        self, ch_dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.create("u64only", ["id Int64", "u64 UInt64"])
        await postgres.execute(
            [
                f"drop table if exists {S}.t_u64",
                f"create table {S}.t_u64 (id bigint, u64 bigint)",
            ]
        )
        outcome = await ch_dags.run(CH_UNSIGNED_INTO_SIGNED)

        assert "narrower than stream uint64" in refused(outcome, "in", "TransferError")


class TestOracleSources:
    @ON_NEWEST
    async def test_number_without_precision_nan_doubles_and_a_big_clob(
        self, ora_dags: PumpDags, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        await oracle.create(
            "EDGES",
            [
                "ID number(10) not null",
                "N number",
                "BD binary_double",
                "TXT clob",
                "VC nvarchar2(20)",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser().name}.edges values "
                "(1, 12345678901234567890123456789012345678, binary_double_nan, "
                "to_clob(rpad('c', 4000, 'c')) || to_clob(rpad('d', 4000, 'd')), "
                "'кириллица 中文')",
                f"insert into {PumpUser().name}.edges values "
                "(2, -1, binary_double_infinity, null, null)",
                f"insert into {PumpUser().name}.edges values "
                "(3, null, 1.5, empty_clob(), '')",
            )
        )
        report = landed(await ora_dags.run(ORA_EDGES))
        loaded = Loaded(postgres, S, "from_ora")
        columns = await loaded.columns()

        assert report.startswith("3 rows loaded")
        assert ("ID", "bigint", True) in columns
        assert ("N", "numeric(38,0)", False) in columns
        assert ("BD", "double precision", False) in columns
        assert ("TXT", "text", False) in columns
        assert (await loaded.scalars('"N"', "ID"))[0] == Decimal(
            "12345678901234567890123456789012345678"
        )
        assert await loaded.texts("BD", "ID") == ["NaN", "Infinity", "1.5"]
        assert (await loaded.scalars('length("TXT")', "ID")) == [8000, None, None]
        assert (await loaded.texts("VC", "ID"))[0] == "кириллица 中文"


COPY_CASES = [
    Case(
        "ts_infinity",
        "timestamp",
        "case g when 1 then 'infinity'::timestamp when 2 then '-infinity' "
        "else timestamp '2024-01-01' end",
    ),
    Case(
        "date_infinity",
        "date",
        "case g when 1 then 'infinity'::date else date '2024-02-29' end",
    ),
    Case(
        "numeric_nan", "numeric(10,2)", "case g when 1 then 'NaN'::numeric else g end"
    ),
    Case("numeric_999", "numeric(999,5)", "12345678901234567890.12345 * g"),
    Case("numeric_free", "numeric", "g / 7.0"),
    Case("range", "int4range", "int4range(1, g + 1)", 90200),
]
"""Что Arrow не несёт, а COPY переносит как есть."""

COPY_EXPECTED = {
    "ts_infinity": ["infinity", "-infinity", "2024-01-01 00:00:00"],
    "date_infinity": ["infinity", "2024-02-29", "2024-02-29"],
    "numeric_nan": ["NaN", "2.00", "3.00"],
    "numeric_999": [
        "12345678901234567890.12345",
        "24691357802469135780.24690",
        "37037036703703703670.37035",
    ],
    "numeric_free": [
        "0.14285714285714285714",
        "0.28571428571428571429",
        "0.42857142857142857143",
    ],
    "range": ["[1,2)", "[1,3)", "[1,4)"],
}


class TestCopyMode:
    """pg -> postgres по COPY csv: то, чего Arrow не несёт, едет как есть."""

    @_applicable(COPY_CASES)
    async def test_values_arrow_cannot_carry_travel_verbatim(
        self, case_dags: PumpDags, postgres: PostgresSide, case: Case
    ) -> None:
        assert postgres.version >= case.min_version, postgres.source.name

        await fill(postgres, f"s_{case.name}", case.kind, case.expr)
        report = landed(await case_dags.run(COPY_VERBATIM))
        target = Loaded(postgres, S, f"t_{case.name}")
        source = Loaded(postgres, S, f"s_{case.name}")

        assert report.startswith(f"{ROWS} rows loaded")
        assert (await target.texts("v"))[:3] == COPY_EXPECTED[case.name]
        assert await target.texts("v") == await source.texts("v")
        assert (await target.columns())[1][1] == (await source.columns())[1][1]

    async def test_enum_needs_a_declared_type_to_stay_an_enum(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        """Имени enum протокол не несёт (только OID): с fallback_as_varchar
        приёмник берёт varchar, с column_types — создаёт колонку типом mood."""
        await fill(postgres, "s_copy_en", "mood", "'sad'::mood")
        landed(await dags.run(COPY_ENUM_AS_VARCHAR))
        landed(await dags.run(COPY_ENUM_TYPED))

        assert (await Loaded(postgres, S, "t_copy_en").columns())[1] == (
            "v",
            "character varying",
            False,
        )
        assert (await Loaded(postgres, S, "t_copy_en_typed").columns())[1] == (
            "v",
            f"{S}.mood",
            False,
        )
        assert (await Loaded(postgres, S, "t_copy_en_typed").texts("v"))[0] == "sad"

    async def test_copy_mode_applies_the_schema_strategies_too(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_copy_bk", "numeric(18,4)", "g / 7.0")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_copy_bk",
                f"create table {S}.t_copy_bk (id bigint not null, v numeric(10,2))",
            ]
        )
        outcome = await dags.run(COPY_NARROWER_TARGET)

        assert "truncates the scale" in refused(outcome, "in", "TransferError")

        report = landed(await dags.run(COPY_NARROWER_TARGET_RECREATED))

        assert "table: dropped and recreated" in report
        assert (await Loaded(postgres, S, "t_copy_bk").columns())[1] == (
            "v",
            "numeric(18,4)",
            False,
        )


EXTENSION_CASES = [
    Case("hstore", "hstore", "hstore('k' || g, 'v \"q\" \\ back')"),
    Case("ltree", "ltree", "('top.node' || g)::ltree"),
    Case("citext", "citext", "('MiXeD' || g)::citext"),
]
"""Типы расширений: реестр psycopg их не знает, тип печатает сервер."""


@_since(EXTENSIONS_SINCE)
class TestUnknownTypes:
    """Тип вне встроенной таблицы psycopg (расширение, как vector у pgvector):
    протокол отдаёт только OID, контракт несёт семейство other без имени,
    значения едут текстом; column_types у приёмника даёт точный тип,
    fallback_as_varchar — varchar, fail_on_unknown (умолчание) — ошибка с тем,
    что известно о типе: OID или объявленное у источника имя."""

    @pytest.fixture(autouse=True)
    async def extensions(self, postgres: PostgresSide) -> None:
        await postgres.ensure_extensions(["hstore", "ltree", "citext"])

    @pytest.mark.parametrize("case", EXTENSION_CASES, ids=lambda c: c.name)
    @pytest.mark.parametrize(
        "mode", [StreamWire.ARROW, StreamWire.CSV], ids=["arrow", "copy"]
    )
    async def test_extension_type_lands_as_varchar_or_as_declared(
        self,
        case_wire_dags: PumpDags,
        postgres: PostgresSide,
        case: Case,
        mode: StreamWire,
    ) -> None:
        name = f"{case.name}_{mode.value}"
        await fill(postgres, f"s_{name}", case.kind, case.expr)
        landed(await case_wire_dags.run(EXTENSION_AS_VARCHAR))
        landed(await case_wire_dags.run(EXTENSION_TYPED))
        source = Loaded(postgres, S, f"s_{name}")
        plain = Loaded(postgres, S, f"t_{name}")
        typed = Loaded(postgres, S, f"t_{name}_typed")

        assert (await plain.columns())[1] == ("v", "character varying", False)
        assert (await typed.columns())[1][1] == (await source.columns())[1][1]
        assert await plain.texts("v") == await source.texts("v")
        assert await typed.texts("v") == await source.texts("v")

    async def test_default_is_refused_with_what_is_known(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        """Без деклараций — ошибка с голым OID от протокола; source_type,
        объявленный LLM у источника, входит в контракт и для pg -> pg
        становится типом колонки."""
        await fill(postgres, "s_hs_asis", "hstore", "hstore('k', 'v')")
        by_oid = r"column v: the target cannot map the source type oid \d+"
        outcome = await dags.run(HSTORE_UNDECLARED)

        assert re.search(by_oid, refused(outcome, "in", "TransferError"))

        landed(await dags.run(HSTORE_DECLARED_AT_SOURCE))
        source = Loaded(postgres, S, "s_hs_asis")
        target = Loaded(postgres, S, "t_hs_asis")

        assert (await target.columns())[1][1] == (await source.columns())[1][1]
        assert await target.texts("v") == await source.texts("v")

    async def test_column_types_for_a_missing_column_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_ct_miss", "int", "g")
        outcome = await dags.run(COLUMN_TYPES_FOR_MISSING_COLUMN)

        assert "column_types: neither the stream" in refused(
            outcome, "in", "ContractError"
        )


class TestVectorTypes:
    """pgvector: тип вне реестра psycopg — контракт несёт только OID,
    значения едут текстом без разбора; тип колонки задают column_types у
    приёмника или source_type у источника, fallback_as_varchar — varchar."""

    @pytest.mark.parametrize(
        "mode", [StreamWire.ARROW, StreamWire.CSV], ids=["arrow", "copy"]
    )
    async def test_vector_lands_as_vector(
        self, vector_wire_dags: PumpDags, vector_db: PostgresSide, mode: StreamWire
    ) -> None:
        name = f"vec_{mode.value}"
        await vector_db.execute(
            [
                f"drop table if exists {S}.s_{name}",
                f"create table {S}.s_{name} "
                "(id bigint not null, v vector(3), h halfvec(2))",
                f"insert into {S}.s_{name} select g, ('[' || g || ',0.5,-1]')::vector, "
                "'[1,2]'::halfvec from generate_series(1, 3) g "
                "union all select 4, null, null",
            ]
        )
        report = landed(await vector_wire_dags.run(VECTOR_TYPED))
        source = Loaded(vector_db, S, f"s_{name}")
        target = Loaded(vector_db, S, f"t_{name}")

        assert report.startswith("4 rows loaded")
        assert (await target.columns())[1:] == [
            ("v", "vector(3)", False),
            ("h", "halfvec(2)", False),
        ]
        assert await target.texts("v") == await source.texts("v")
        assert (await target.texts("v"))[3] is None

    async def test_varchar_fallback_keeps_the_vector_text(
        self, vector_dags: PumpDags, vector_db: PostgresSide
    ) -> None:
        await vector_db.execute(
            [
                f"drop table if exists {S}.s_vec_ddl",
                f"create table {S}.s_vec_ddl (id bigint not null, v vector(3))",
                f"insert into {S}.s_vec_ddl values (1, '[1,2,3]')",
            ]
        )
        landed(await vector_dags.run(VECTOR_AS_VARCHAR))
        loaded = Loaded(vector_db, S, "t_vec_ddl")

        assert (await loaded.columns())[1] == ("v", "character varying", False)
        assert (await loaded.texts("v"))[0] == "[1,2,3]"


class TestBackupNames:
    @ON_NEWEST
    async def test_two_backups_in_a_row_get_distinct_names(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_bk", "int", "g")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_bk",
                f"create table {S}.t_bk (id bigint not null, old text)",
            ]
        )
        first = landed(await dags.run(BACKUP_AND_CREATE))
        second = landed(await dags.run(BACKUP_AND_CREATE))
        backups = [
            t
            for t in await Loaded(postgres, S, "t_bk").tables()
            if t.startswith("t_bk_bak_")
        ]

        assert "saved as" in first
        assert "t_bk_bak_" in first
        assert "t_bk_bak_" in second
        assert len(backups) == 2
