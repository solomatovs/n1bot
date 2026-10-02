# ruff: noqa: S608, E501, PLR0913
"""Загрузка postgres -> postgres парой boba-stream-pg-to-pg через pg_stream_out и
pg_stream_in с раскладками csv, tsv и binary: контракт RowDescription как есть, сверка
по OID и typmod с каталогом приёмника, DDL текстом типа источника, тела COPY
без перекодирования. Прогон по всем postgres стенда и Greenplum.

Каждый запуск насосов описан toml-текстом (DagSpec) и исполняется DagRunner
без модели: узлы — вызовы насосов с аргументами, как их присылает модель.
Подстановкой в описания идёт только имя схемы стенда ($s).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import psycopg
import pytest

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.query import PgQueryBuilder
from boba.db.postgres.transfer import (
    PgCatalogColumn,
    PgCopyLayout,
    PgCopyOut,
    PgTypeRules,
)
from boba.pump_stand import Loaded, PostgresSide, PumpDags, PumpStand
from boba.pump_stand.ports import Sink, SinkOutbound
from boba.stand.names import StandNames
from boba.stream.pg_to_pg.transfer import PgStreamColumn
from boba.toolkit.result import SqlFailureResult
from boba.toolkit.transfer import CommandJournal, TransferError, TransferOutbound
from boba.toolrun.dag_run import DagOutcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
S = StandNames().of("pump_transfer")
ROWS = 60

CREATE = """
name = "pg_transfer_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_create"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""
"""Источник -> приёмник на одном сервере, id объявлен not null."""

TWICE_CREATE = """
name = "pg_transfer_twice_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_twice"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TWICE_RELOAD = """
name = "pg_transfer_twice_reload"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_twice"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ABSENT = """
name = "pg_transfer_absent"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_absent"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

NARROW_REFUSED = """
name = "pg_transfer_narrow_refused"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_narrow"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

NARROW_RECREATED = """
name = "pg_transfer_narrow_recreated"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_narrow"
schema_strategy = { kind = "drop_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

BACKUP = """
name = "pg_transfer_backup"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_bak"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

DELETE_CREATE = """
name = "pg_transfer_delete_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_del"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

DELETE_WHERE_INSERT_NOTHING = """
name = "pg_transfer_delete_where_insert_nothing"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_del"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "id <= 20" }
insert_strategy = { kind = "nothing" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

DELETE_ALL = """
name = "pg_transfer_delete_all"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_del"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_all" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

RULES = """
name = "pg_transfer_rules"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_rules"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { title = "name" }, column_types = { amount = "numeric(20,6)" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

DECLARED_TARGET_TYPE = """
name = "pg_transfer_declared_target_type"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, amount as v, 1 as extra from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_dec"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { v = "numeric(20,6)" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

UNKNOWN_DECLARED_TYPE = """
name = "pg_transfer_unknown_declared_type"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_bad"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { note = "no_such" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

RENAME_MISSING = """
name = "pg_transfer_rename_missing"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_miss"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { x = "nope" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

COLUMN_TYPES_MISSING = """
name = "pg_transfer_column_types_missing"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_miss2"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { x = "int" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

DECLARATION_MISSING = """
name = "pg_transfer_declaration_missing"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }, { name = "nope", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_miss3"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_REFUSED = """
name = "pg_transfer_enum_refused"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_AS_VARCHAR = """
name = "pg_transfer_enum_as_varchar"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en2"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en_var"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_BY_COLUMN_TYPES = """
name = "pg_transfer_enum_by_column_types"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en2"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en_typed"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { v = "$s.mood" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_BY_TYPE_TEXT = """
name = "pg_transfer_enum_by_type_text"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en2"
wire = "csv"
columns = [{ name = "id", nullable = false }, { name = "v", type_text = "$s.mood" }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en_named"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_UNNAMED = """
name = "pg_transfer_enum_unnamed"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en3"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en3"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_NAMED_SAME = """
name = "pg_transfer_enum_named_same"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en4"
wire = "csv"
columns = [{ name = "id", nullable = false }, { name = "v", type_text = "$s.mood" }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en4"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ENUM_NAMED_OTHER = """
name = "pg_transfer_enum_named_other"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_en4"
wire = "csv"
columns = [{ name = "id", nullable = false }, { name = "v", type_text = "$s.mood" }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_en4_other"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

EXACT_MISMATCH = """
name = "pg_transfer_exact_mismatch"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_exact"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_exact"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""
"""Пара таблиц одна на все случаи матрицы: тест пересоздаёт обе."""

EXACT_WIDER = """
name = "pg_transfer_exact_wider"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_wider"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_wider"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

ALL_BUILTIN_TYPES = """
name = "pg_transfer_all_builtin_types"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_all"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_all"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

OLDER_TARGET = """
name = "pg_transfer_older_target"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_old"
wire = "csv"
columns = []
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg_older"
schema_name = "$s"
table_name = "t_old"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TEMPLATE_WITH_OPTIONS = """
name = "pg_transfer_template_with_options"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tpl_with"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) with (fillfactor = 70)"
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TEMPLATE_DISTRIBUTED = """
name = "pg_transfer_template_distributed"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tpl_dist"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) distributed by (id)"
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TEMPLATE_ESCAPED_BRACES = """
name = "pg_transfer_template_escaped_braces"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tpl_esc"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) -- {{not a variable}}"
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TEMPLATE_UNKNOWN_TABLESPACE = """
name = "pg_transfer_template_unknown_tablespace"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tpl_unknown"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) tablespace {tablespace}"
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TEMPLATE_UNKNOWN_OWNER = """
name = "pg_transfer_template_unknown_owner"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tpl_unknown"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) tablespace {owner}"
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TEMPLATE_BAD_OPTION = """
name = "pg_transfer_template_bad_option"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "csv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tpl_bad"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) with (no_such_option = 1)"
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TSV = """
name = "pg_transfer_tsv"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_tsv"
wire = "tsv"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_tsv"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

BINARY = """
name = "pg_transfer_binary"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "binary"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_bin"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

BINARY_ENUM = """
name = "pg_transfer_binary_enum"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.s_bin_en"
wire = "binary"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$s"
table_name = "t_bin_en"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

BINARY_OTHER_MAJOR = """
name = "pg_transfer_binary_other_major"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $s.src"
wire = "binary"
columns = []
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg_older"
schema_name = "$s"
table_name = "t_bin_old"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""


def _newest(sources: Sequence[Any]) -> str:
    plain: list[str] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source.name)

    return plain[-1]


NEWEST = _newest(STAND.sources)


def _older_than(version: int) -> Any:
    """Самый новый postgres стенда ниже данной версии сервера (по имени
    pg-<major>), None — такого нет."""
    chosen: Any = None
    for source in STAND.sources:
        if not source.name.startswith("pg-"):
            continue

        major = source.name.removeprefix("pg-")
        if float(major) * 10000 < version:
            chosen = source

    return chosen


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, S)
    await side.connect()
    await side.recreate_schema(
        [
            "create type mood as enum ('sad', 'happy')",
            "create type mood2 as enum ('sad', 'happy')",
            f"""
            create table {S}.src (
                id bigint not null,
                name varchar(50) not null,
                amount numeric(18,4),
                dt timestamp(3),
                flag boolean,
                note text
            )""",
            f"""
            insert into {S}.src
            select g, 'name ' || g, g / 7.0,
                   timestamp '2024-01-01' + g * interval '1 min',
                   g % 2 = 0, case when g % 5 = 0 then null else 'note ' || g end
            from generate_series(1, {ROWS}) g""",
        ]
    )

    yield side

    await side.drop()


@pytest.fixture
def dags(tmp_path: Path, postgres: PostgresSide) -> PumpDags:
    """Запуск описаний источник -> приёмник на одном сервере."""
    return PumpDags(tmp_path, {"pg": postgres.profile}, {"s": S})


def only_newest(postgres: PostgresSide) -> None:
    if postgres.source.name != NEWEST:
        pytest.skip("one postgres is enough here")


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
    await postgres.execute(
        [
            f"drop table if exists {S}.{table}",
            f"create table {S}.{table} (id bigint not null, v {kind})",
            f"insert into {S}.{table} select g, {expr} from generate_series(1, 3) g "
            "union all select 4, null",
        ]
    )


async def existing(postgres: PostgresSide, table: str, kind: str) -> None:
    await postgres.execute(
        [
            f"drop table if exists {S}.{table}",
            f"create table {S}.{table} (id bigint not null, v {kind})",
        ]
    )


class TestSchemaStrategies:
    async def test_create_keeps_the_source_types(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        report = landed(await dags.run(CREATE))
        landed_table = Loaded(postgres, S, "t_create")

        source_types = [
            column[:2] for column in await Loaded(postgres, S, "src").columns()
        ]
        landed_columns = await landed_table.columns()

        assert f"{ROWS} rows loaded into {S}.t_create" in report
        assert "table: created (table is missing)" in report
        assert [column[:2] for column in landed_columns] == source_types
        assert landed_columns[0] == ("id", "bigint", True)
        assert landed_columns[1] == ("name", "character varying(50)", False)
        assert await landed_table.count() == ROWS
        assert await landed_table.texts("amount") == await Loaded(
            postgres, S, "src"
        ).texts("amount")

    async def test_second_load_keeps_and_reports_the_check(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(TWICE_CREATE))
        report = landed(await dags.run(TWICE_RELOAD))

        assert "table: kept as is (schema matches)" in report
        assert '"column": "amount"' in report
        assert "removed by truncate" in report
        assert "statement: truncate table" in report
        assert await Loaded(postgres, S, "t_twice").count() == ROWS

    async def test_error_if_not_exists_refuses_a_missing_table(
        self, dags: PumpDags
    ) -> None:
        outcome = await dags.run(ABSENT)

        assert "table is missing" in refused(outcome, "in", "TransferError")

    async def test_narrower_column_is_a_schema_change(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.t_narrow",
                f"create table {S}.t_narrow (id bigint not null, name varchar(10), "
                "amount numeric(18,4), dt timestamp(3), flag boolean, note text)",
            ]
        )
        outcome = await dags.run(NARROW_REFUSED)

        assert "is shorter than stream" in refused(outcome, "in", "TransferError")

        report = landed(await dags.run(NARROW_RECREATED))

        assert "table: dropped and recreated" in report
        assert ("name", "character varying(50)", False) in await Loaded(
            postgres, S, "t_narrow"
        ).columns()

    async def test_backup_renames_the_old_table(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.t_bak",
                f"create table {S}.t_bak (id bigint not null, extra int)",
            ]
        )
        report = landed(await dags.run(BACKUP))
        tables = await Loaded(postgres, S, "t_bak").tables()

        assert "table: recreated (" in report
        assert "saved as" in report
        assert "t_bak_bak_" in report
        assert any(name.startswith("t_bak_bak_") for name in tables)


class TestDeleteAndInsert:
    async def test_delete_all_and_where_and_insert_nothing(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        part = ROWS // 3
        landed(await dags.run(DELETE_CREATE))
        report = landed(await dags.run(DELETE_WHERE_INSERT_NOTHING))

        assert f"{part} rows deleted" in report
        assert "statement: delete from" in report
        assert "0 rows loaded" in report
        assert await Loaded(postgres, S, "t_del").count() == ROWS - part

        report = landed(await dags.run(DELETE_ALL))

        assert f"{ROWS - part} rows deleted" in report
        assert "statement: delete from" in report
        assert await Loaded(postgres, S, "t_del").count() == ROWS


class TestRulesAndDeclarations:
    async def test_rename_and_column_types_shape_the_ddl(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(RULES))
        columns = await Loaded(postgres, S, "t_rules").columns()

        assert ("title", "character varying(50)", False) in columns
        assert ("amount", "numeric(20,6)", False) in columns
        assert (await Loaded(postgres, S, "t_rules").scalars("amount"))[0] == Decimal(
            "0.142900"
        )

    async def test_declared_target_type_takes_part_in_the_check(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await existing(postgres, "t_dec", "numeric(18,4)")
        await postgres.execute([f"alter table {S}.t_dec add column extra int"])
        outcome = await dags.run(DECLARED_TARGET_TYPE)

        assert "truncates the scale of stream" in refused(
            outcome, "in", "TransferError"
        )

    async def test_unknown_declared_type_is_refused_before_ddl(
        self, dags: PumpDags
    ) -> None:
        outcome = await dags.run(UNKNOWN_DECLARED_TYPE)

        assert "does not accept the declared types" in refused(
            outcome, "in", "TransferError"
        )

    async def test_rule_on_a_missing_column_is_refused(self, dags: PumpDags) -> None:
        outcome = await dags.run(RENAME_MISSING)

        assert "has no field 'nope'" in refused(outcome, "in", "TransferError")

        outcome = await dags.run(COLUMN_TYPES_MISSING)

        assert "column_types: neither the stream" in refused(
            outcome, "in", "TransferError"
        )

    async def test_declaration_on_a_missing_column_is_refused(
        self, dags: PumpDags
    ) -> None:
        outcome = await dags.run(DECLARATION_MISSING)

        assert "has no column 'nope'" in refused(outcome, "out", "TransferError")


class TestUnknownTypes:
    """enum: сервер отдаёт только OID, текста типа нет — ошибка с подсказкой,
    varchar по стратегии, точный тип по column_types или по type_text."""

    async def test_enum_without_a_type_is_refused_with_the_oid(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_en", "mood", "'sad'::mood")
        outcome = await dags.run(ENUM_REFUSED)

        assert re.search(
            r"source type oid \d+; declare", refused(outcome, "in", "TransferError")
        )

    async def test_enum_lands_as_varchar_or_as_declared(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_en2", "mood", "'happy'::mood")
        landed(await dags.run(ENUM_AS_VARCHAR))
        landed(await dags.run(ENUM_BY_COLUMN_TYPES))
        landed(await dags.run(ENUM_BY_TYPE_TEXT))
        source = Loaded(postgres, S, "s_en2")

        assert (await Loaded(postgres, S, "t_en_var").columns())[1] == (
            "v",
            "character varying",
            False,
        )
        assert (await Loaded(postgres, S, "t_en_typed").columns())[1][1] == f"{S}.mood"
        assert (await Loaded(postgres, S, "t_en_named").columns())[1][1] == f"{S}.mood"
        assert await Loaded(postgres, S, "t_en_named").texts("v") == await source.texts(
            "v"
        )

    async def test_unnamed_enum_cannot_be_verified_but_loads(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        """OID enum источника на приёмнике не значит ничего, даже если это
        тот же сервер: без имени — предупреждение, данные едут текстом."""
        await fill(postgres, "s_en3", "mood", "'sad'::mood")
        await existing(postgres, "t_en3", "mood")
        report = landed(await dags.run(ENUM_UNNAMED))

        assert "type cannot be verified, the source named no type" in report
        assert await Loaded(postgres, S, "t_en3").texts("v") == [
            "sad",
            "sad",
            "sad",
            None,
        ]

    async def test_named_enum_is_compared_by_name_on_the_target(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        """Имя типа из контракта приёмник разбирает у себя и сравнивает свой
        OID с каталогом: тот же enum — ok, другой enum с теми же метками —
        ошибка."""
        await fill(postgres, "s_en4", "mood", "'happy'::mood")
        await existing(postgres, "t_en4", "mood")
        await existing(postgres, "t_en4_other", "mood2")
        report = landed(await dags.run(ENUM_NAMED_SAME))

        assert '"column": "v"' in report

        outcome = await dags.run(ENUM_NAMED_OTHER)

        assert f"type differs: stream {S}.mood, table" in refused(
            outcome, "in", "TransferError"
        )


class TestExactTypes:
    """Сверка по OID и typmod: другой тип — ошибка, уже typmod — ошибка,
    шире — предупреждение."""

    @pytest.fixture(autouse=True)
    def newest(self, postgres: PostgresSide) -> None:
        only_newest(postgres)

    @pytest.mark.parametrize(
        ("source_kind", "expr", "target_kind", "expected"),
        [
            (
                "uuid",
                "gen_random_uuid()",
                "text",
                "type differs: stream uuid, table text",
            ),
            ("json", "'{}'::json", "jsonb", "type differs: stream json, table jsonb"),
            ("time(6)", "time '12:34:56.123456'", "time(0)", "is coarser than stream"),
            ("timetz", "timetz '12:00:00+03'", "time", "type differs"),
            (
                "inet",
                "'10.0.0.1'::inet",
                "cidr",
                "type differs: stream inet, table cidr",
            ),
            ("int4range", "int4range(1, 3)", "int8range", "type differs"),
            ("bit(8)", "B'10101010'", "bit(4)", "is shorter than stream"),
            ("numeric(20,6)", "g / 7.0", "numeric(18,4)", "truncates the scale"),
            ("text", "'abc'", "varchar(20)", "type differs"),
        ],
    )
    async def test_mismatch_is_refused(
        self,
        dags: PumpDags,
        postgres: PostgresSide,
        source_kind: str,
        expr: str,
        target_kind: str,
        expected: str,
    ) -> None:
        await fill(postgres, "s_exact", source_kind, expr)
        await existing(postgres, "t_exact", target_kind)
        outcome = await dags.run(EXACT_MISMATCH)

        assert expected in refused(outcome, "in", "TransferError")

    @pytest.mark.parametrize(
        ("source_kind", "expr", "target_kind", "expected"),
        [
            ("varchar(10)", "'abc'", "varchar(20)", "is longer than stream"),
            ("numeric(18,4)", "g / 7.0", "numeric(20,6)", "is wider than stream"),
            ("timestamp(0)", "now()", "timestamp(6)", "is finer than stream"),
        ],
    )
    async def test_wider_target_only_warns(
        self,
        dags: PumpDags,
        postgres: PostgresSide,
        source_kind: str,
        expr: str,
        target_kind: str,
        expected: str,
    ) -> None:
        await fill(postgres, "s_wider", source_kind, expr)
        await existing(postgres, "t_wider", target_kind)
        report = landed(await dags.run(EXACT_WIDER))

        assert '"column": "v"' in report
        assert expected in report

    async def test_created_table_keeps_every_builtin_type(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.s_all",
                f"create table {S}.s_all (id bigint not null, u uuid, j json, "
                "jb jsonb, b bytea, t time(3), tz timetz, iv interval, ip inet, "
                "mac macaddr, m money, r int4range, a text[], bt bit(3), "
                "vb varbit(8), x xml, tsv tsvector, pt point, pg polygon, "
                "lsn pg_lsn, rc regclass, n numeric, d date, f real, dp float8)",
                f"insert into {S}.s_all values (1, gen_random_uuid(), '{{}}', "
                "'{}', '\\\\x00', '01:02:03.123', '01:02:03+03', '1 day', "
                "'10.0.0.1', '08:00:2b:01:02:03', 1.5, '[1,3)', array['a'], "
                "B'101', B'1', '<a/>', 'a b', '(1,2)', '((0,0),(1,1),(1,0))', "
                "'0/16B3748', 'pg_class', 1.5, '2024-01-01', 1.5, 1e300)",
            ]
        )
        landed(await dags.run(ALL_BUILTIN_TYPES))
        source = Loaded(postgres, S, "s_all")
        target = Loaded(postgres, S, "t_all")

        assert await target.columns() == await source.columns()
        names = [column[0] for column in await source.columns()][1:]
        for column in names:
            assert await target.texts(column) == await source.texts(column)


class TestOlderTarget:
    """Тип источника, которого нет на версии приёмника: OID встроенных типов
    одинаковы на всех версиях, но самого типа на старом сервере нет — отказ
    с подсказкой от приёмника до загрузки."""

    async def test_missing_type_on_the_target_is_a_clear_error(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        if postgres.version < 140000:
            pytest.skip("multirange needs 14")

        older = _older_than(140000)
        if older is None:
            pytest.skip("no postgres older than 14 on the stand")

        target = PostgresSide(older, S)
        await target.connect()
        await target.recreate_schema()
        await fill(postgres, "s_old", "int4multirange", "'{[1,3)}'::int4multirange")
        dags = PumpDags(
            tmp_path, {"pg": postgres.profile, "pg_older": target.profile}, {"s": S}
        )
        try:
            outcome = await dags.run(OLDER_TARGET)

            assert 'no such type: type "int4multirange" does not exist' in refused(
                outcome, "in", "TransferError"
            )
            assert await Loaded(target, S, "t_old").tables() == []
        finally:
            await target.drop()


class TestCreateTemplate:
    """Шаблон create table: особенности таблицы пишет вызывающий, приёмник
    подставляет экранированные схему, имя и колонки; шаблон без
    обязательной переменной или с чужой — отказ до любого DDL."""

    async def test_with_options_reach_reloptions(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        report = landed(await dags.run(TEMPLATE_WITH_OPTIONS))
        landed_table = Loaded(postgres, S, "t_tpl_with")

        assert f"{ROWS} rows loaded" in report
        assert await landed_table.count() == ROWS
        options = await landed_table.aggregate(
            "(select reloptions::text from pg_class "
            f"where oid = '{S}.t_tpl_with'::regclass)"
        )
        assert options == "{fillfactor=70}"

    async def test_distributed_by_on_greenplum(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        if not postgres.greenplum:
            pytest.skip("distributed by is Greenplum only")

        report = landed(await dags.run(TEMPLATE_DISTRIBUTED))
        landed_table = Loaded(postgres, S, "t_tpl_dist")

        assert f"{ROWS} rows loaded" in report
        policy = await landed_table.aggregate(
            "(select distkey::text from gp_distribution_policy "
            f"where localoid = '{S}.t_tpl_dist'::regclass)"
        )
        assert policy == "1"

    async def test_escaped_braces_stay_literal(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        report = landed(await dags.run(TEMPLATE_ESCAPED_BRACES))

        assert f"{ROWS} rows loaded" in report
        assert await Loaded(postgres, S, "t_tpl_esc").count() == ROWS

    async def test_template_with_an_unknown_variable_is_refused(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        outcome = await dags.run(TEMPLATE_UNKNOWN_TABLESPACE)

        assert "uses {tablespace}" in refused(outcome, "in", "TransferError")
        assert (
            "t_tpl_no_cols" not in await Loaded(postgres, S, "t_tpl_no_cols").tables()
        )

    async def test_unknown_variable_is_refused(self, dags: PumpDags) -> None:
        outcome = await dags.run(TEMPLATE_UNKNOWN_OWNER)

        assert "create_table uses {owner}, but the receiver has no such value" in (
            refused(outcome, "in", "TransferError")
        )

    async def test_server_error_in_options_is_reported(self, dags: PumpDags) -> None:
        """Ошибка сервера на create table: сбой приёмника с упавшей командой
        журнала последней."""
        outcome = await dags.run(TEMPLATE_BAD_OPTION)

        failure = outcome.failure("in")
        assert isinstance(failure, SqlFailureResult), failure
        assert "no_such_option" in failure.llm_view()
        assert failure.statements[-1].status.startswith("failed: ")


class TestTsvWire:
    async def test_text_layout_lands_the_same_rows(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_tsv",
            "text",
            "'tab\\there' || chr(10) || 'back\\\\slash' || g",
        )
        report = landed(await dags.run(TSV))

        assert "4 rows loaded" in report
        assert await Loaded(postgres, S, "t_tsv").texts("v") == await Loaded(
            postgres, S, "s_tsv"
        ).texts("v")


class TestBinaryWire:
    """COPY binary: тот же сервер — грузится байт в байт; enum — отказ, у
    пользовательских типов в binary OID инстанса; другая мажорная версия —
    отказ с подсказкой взять csv."""

    async def test_binary_lands_the_same_rows_on_the_same_server(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        report = landed(await dags.run(BINARY))
        source = Loaded(postgres, S, "src")
        target = Loaded(postgres, S, "t_bin")

        assert f"{ROWS} rows loaded" in report
        for column in ("name", "amount", "dt", "flag", "note"):
            assert await target.texts(column) == await source.texts(column)

    async def test_user_type_is_refused_in_binary(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_bin_en", "mood", "'sad'::mood")
        outcome = await dags.run(BINARY_ENUM)

        assert "outside the built-in registry" in refused(
            outcome, "in", "TransferError"
        )

    async def test_other_major_version_is_refused(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        older = _older_than(postgres.version // 10000 * 10000)
        if older is None:
            pytest.skip("no older postgres on the stand")

        target = PostgresSide(older, S)
        await target.connect()
        await target.recreate_schema()
        dags = PumpDags(
            tmp_path, {"pg": postgres.profile, "pg_older": target.profile}, {"s": S}
        )
        try:
            outcome = await dags.run(BINARY_OTHER_MAJOR)

            assert "major versions differ" in refused(outcome, "in", "TransferError")
        finally:
            await target.drop()


class TestDescribeCost:
    """Контракт берётся PQprepare + PQdescribePrepared без выполнения: запрос
    на часы выполнения описывается за миллисекунды, а счётчики чтения базы
    не растут."""

    HEAVY: ClassVar[str] = (
        "with recursive r(n) as (select 1 union all select n + 1 from r "
        "where n < 200000000) "
        "select r.n, pg_sleep(0.01) as slept, md5(r.n::text) as h, "
        "sum(r.n) over () as total from r "
        "join generate_series(1, 1000000) g on g = r.n"
    )
    COUNTERS: ClassVar[str] = (
        "select tup_returned, tup_fetched, blks_read from pg_stat_database "
        "where datname = current_database()"
    )
    BUDGET_SECONDS: ClassVar[float] = 0.5

    async def test_describe_neither_plans_nor_reads(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            counters = PgQueryBuilder().add(self.COUNTERS).build()
            cursor = await conn.execute(counters.text)
            before = await cursor.fetchone()
            started = time.perf_counter()
            contract = await PgCopyOut(conn, CommandJournal("test")).contract(
                self.HEAVY, ()
            )
            elapsed = time.perf_counter() - started
            cursor = await conn.execute(counters.text)
            after = await cursor.fetchone()

        names = [column.name for column in contract.columns]

        assert names == ["n", "slept", "h", "total"]
        assert elapsed < self.BUDGET_SECONDS
        assert after == before


class BrokenSink(Sink):
    """Порт, который рвёт трубу на втором кадре rows."""

    LIMIT: ClassVar[int] = 2

    def emit(self, head: Any, body: Any = b"") -> None:
        super().emit(head, body)
        if len(self.heads) >= self.LIMIT:
            raise BrokenPipeError("receiver went away")


class TestCopyOutLoop:
    """COPY TO STDOUT идёт циклом libpq в потоке: миллион строк не медленнее
    полутора psql, ошибка сервера посреди COPY доходит своим классом,
    обрыв трубы отменяет запрос, и соединение остаётся рабочим."""

    ROWS: ClassVar[int] = 1_000_000
    SELECT: ClassVar[str] = (
        "select g as id, g * 7 as v, 'name_' || g as name, now() as ts, "
        "g::numeric / 3 as amount from generate_series(1, {rows}) g"
    )
    CHUNK: ClassVar[int] = 262144
    PSQL_RATIO: ClassVar[float] = 1.5

    def _psql_seconds(self, postgres: PostgresSide, select: str) -> float:
        psql = shutil.which("psql")
        if psql is None:
            pytest.skip("psql is not installed on the host")

        settings = postgres.profile.conn_settings()
        env = {"PGPASSWORD": str(settings.get("password", "")), "PATH": "/usr/bin:/bin"}
        argv = [
            psql,
            "-h",
            str(settings["host"]),
            "-p",
            str(settings["port"]),
            "-U",
            str(settings["user"]),
            "-d",
            str(settings["dbname"]),
            "-c",
            f"\\copy ({select}) to '/dev/null' with (format csv)",
        ]
        started = time.perf_counter()
        completed = subprocess.run(
            argv, env=env, capture_output=True, text=True, check=False
        )
        elapsed = time.perf_counter() - started

        assert completed.returncode == 0, completed.stderr
        assert f"COPY {self.ROWS}" in completed.stdout

        return elapsed

    async def test_million_rows_keep_up_with_psql(self, postgres: PostgresSide) -> None:
        only_newest(postgres)
        select = self.SELECT.format(rows=self.ROWS)
        sink = Sink()
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            copy_out = PgCopyOut(conn, CommandJournal("test"))
            contract = await copy_out.contract(select, ())
            started = time.perf_counter()
            report = await copy_out.stream(
                select,
                PgCopyLayout.CSV,
                contract,
                self.CHUNK,
                TransferOutbound(SinkOutbound(sink)),
            )
            elapsed = time.perf_counter() - started

        psql_elapsed = self._psql_seconds(postgres, select)
        print(
            f"\ncopy out {self.ROWS} rows: ours {elapsed:.2f}s "
            f"({self.ROWS / elapsed:,.0f} rows/s), psql {psql_elapsed:.2f}s, "
            f"frames {len(sink.heads) - 1}"
        )

        assert report.status == f"COPY {self.ROWS}"
        assert sink.data().count(b"\n") == self.ROWS
        assert len(sink.heads) - 1 <= len(sink.data()) // self.CHUNK + 1
        assert elapsed <= psql_elapsed * self.PSQL_RATIO

    async def test_server_error_mid_copy_keeps_its_class(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        select = "select g, 1 / (g - 5000) as bad from generate_series(1, 10000) g"
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            copy_out = PgCopyOut(conn, CommandJournal("test"))
            contract = await copy_out.contract(select, ())
            with pytest.raises(psycopg.errors.DivisionByZero):
                await copy_out.stream(
                    select,
                    PgCopyLayout.CSV,
                    contract,
                    4096,
                    TransferOutbound(SinkOutbound(Sink())),
                )

            await conn.rollback()
            cursor = await conn.execute("select 1")

            assert await cursor.fetchone() == (1,)

    async def test_broken_pipe_cancels_and_frees_the_connection(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        select = self.SELECT.format(rows=self.ROWS)
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            copy_out = PgCopyOut(conn, CommandJournal("test"))
            contract = await copy_out.contract(select, ())
            started = time.perf_counter()
            with pytest.raises(BrokenPipeError):
                await copy_out.stream(
                    select,
                    PgCopyLayout.CSV,
                    contract,
                    4096,
                    TransferOutbound(SinkOutbound(BrokenSink())),
                )
            elapsed = time.perf_counter() - started

            await conn.rollback()
            cursor = await conn.execute("select 2")

            assert await cursor.fetchone() == (2,)

        assert elapsed < 1.0


class TestTypeRules:
    """Правила сверки postgres на встроенном реестре psycopg без сервера."""

    NUMERIC: ClassVar[int] = 1700
    VARCHAR: ClassVar[int] = 1043
    TEXT: ClassVar[int] = 25
    TIMESTAMP: ClassVar[int] = 1114

    @staticmethod
    def stream(oid: int, typmod: int, nullable: bool = True) -> PgStreamColumn:
        return PgStreamColumn(
            name="v",
            oid=oid,
            typmod=typmod,
            type_text="t",
            known="t",
            nullable=nullable,
            resolved=False,
        )

    @staticmethod
    def table(oid: int, typmod: int, not_null: bool = False) -> PgCatalogColumn:
        return PgCatalogColumn(
            position=1,
            name="v",
            oid=oid,
            typmod=typmod,
            table_oid=0,
            attnum=2,
            size=-1,
            not_null=not_null,
        )

    @staticmethod
    def numeric(precision: int, scale: int) -> int:
        return ((precision << 16) | scale) + 4

    @pytest.mark.parametrize(
        ("stream", "table", "level", "message"),
        [
            ((TEXT, -1), (TEXT, -1), "ok", "ok"),
            ((TEXT, -1), (VARCHAR, 24), "error", "type differs"),
            ((VARCHAR, 14), (VARCHAR, 24), "warning", "is longer than stream"),
            ((VARCHAR, 24), (VARCHAR, 14), "error", "is shorter than stream"),
            ((VARCHAR, -1), (VARCHAR, 14), "warning", "limits the length"),
            ((TIMESTAMP, 0), (TIMESTAMP, -1), "warning", "is finer than stream"),
            ((TIMESTAMP, -1), (TIMESTAMP, 0), "error", "is coarser than stream"),
        ],
    )
    def test_compare(
        self, stream: tuple[int, int], table: tuple[int, int], level: str, message: str
    ) -> None:
        from psycopg.postgres import types as registry

        verdict = PgTypeRules(registry).compare(
            self.stream(*stream), self.table(*table)
        )

        assert verdict.level.value == level
        assert message in verdict.message

    def test_numeric_digits(self) -> None:
        from psycopg.postgres import types as registry

        rules = PgTypeRules(registry)
        wider = rules.compare(
            self.stream(self.NUMERIC, self.numeric(18, 4)),
            self.table(self.NUMERIC, self.numeric(20, 6)),
        )
        scale = rules.compare(
            self.stream(self.NUMERIC, self.numeric(20, 6)),
            self.table(self.NUMERIC, self.numeric(18, 4)),
        )
        digits = rules.compare(
            self.stream(self.NUMERIC, self.numeric(20, 4)),
            self.table(self.NUMERIC, self.numeric(18, 4)),
        )

        assert wider.level.value == "warning"
        assert "truncates the scale" in scale.message
        assert "fewer integer digits" in digits.message

    def test_nullable(self) -> None:
        from psycopg.postgres import types as registry

        rules = PgTypeRules(registry)
        refused_verdict = rules.compare(
            self.stream(self.TEXT, -1), self.table(self.TEXT, -1, True)
        )
        warned = rules.compare(
            self.stream(self.TEXT, -1, nullable=False), self.table(self.TEXT, -1)
        )

        assert refused_verdict.level.value == "error"
        assert warned.level.value == "warning"


class TestRegistryHint:
    def test_missing_pair_is_a_clear_error(self) -> None:
        from boba.db.postgres.transfer import PgTransfers
        from boba.toolkit.transfer import Engine

        with pytest.raises(
            TransferError, match="no transfer from oracle into postgres"
        ):
            PgTransfers({}).pair(Engine.ORACLE)
