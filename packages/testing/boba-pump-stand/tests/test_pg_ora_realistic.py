# ruff: noqa: S608, E501
"""Перелив отчёта по заказам из postgres и Greenplum в Oracle парой
postgres -> Oracle (pg_stream_out с wire = arrow и ora_stream_in) запросом,
каким его написал бы LLM: CTE, join и left join с NULL, оконные функции,
агрегаты в строку, enum, массив, uuid, inet, interval, date_trunc.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Источник — каждый postgres (9.0–19) и Greenplum (6, 7) стенда, приёмник —
каждый Oracle стенда. Запрос отчёта держится возможностей 9.0. Сценарии
идут по порядку на каждой паре и опираются на таблицы предыдущих:
    - первая попытка с enum и массивом как есть: отказ, затем CLOB по
      fallback_as_varchar;
    - загрузка отчёта с типами Oracle по контракту, повторная с проверкой
      схемы;
    - инкремент одного месяца через delete_where;
    - витрина приёмника со своими именами через rename_columns и column_types
      (VARCHAR2 вместо CLOB);
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table с tablespace и nologging;
    - подмена витрины шагами after через rename, откат строк при ошибке шага;
    - сухой прогон do_nothing + insert nothing;
    - json и jsonb источника — CLOB, читаются json-функциями Oracle;
    - обратный путь: агрегат «город × месяц» из Oracle в postgres.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_pg_ch_realistic import (
    FILL_DML,
    ORDERS,
    RAW_SQL,
    REPORT_SQL,
    SCHEMA_DDL,
    SRC,
)

from boba.pump_stand import (
    Loaded,
    OracleSide,
    OraLoaded,
    PostgresSide,
    PumpDags,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.toolkit.result import FailureResult, SqlFailureResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
OWNER = PumpUser().name
ARRAYSIZE = 2000
BOOLEAN_SINCE = 23
PG_JSON_SINCE = 90400
MONTH = "2024-03-01"

EXPECTED_COLUMNS = [
    ("order_id", "NUMBER(19,0)", True),
    ("status", "CLOB", False),
    ("customer_uid", "VARCHAR2(36 CHAR)", False),
    ("customer_name", "CLOB", False),
    ("email", "CLOB", False),
    ("tier", "CLOB", False),
    ("city", "CLOB", False),
    ("tags", "CLOB", False),
    ("last_ip", "CLOB", False),
    ("is_active", "{boolean}", False),
    ("placed_at", "TIMESTAMP(6) WITH TIME ZONE", True),
    ("month", "DATE", True),
    ("shipped_at", "TIMESTAMP(6) WITH TIME ZONE", False),
    ("lead_time", "CLOB", False),
    ("note", "CLOB", False),
    ("lines", "NUMBER(19,0)", False),
    ("units", "NUMBER(19,0)", False),
    ("gross", "NUMBER(14,2)", False),
    ("categories", "CLOB", False),
    ("paid", "NUMBER(14,2)", False),
    ("paid_at", "TIMESTAMP(6) WITH TIME ZONE", False),
    ("methods", "CLOB", False),
    ("payment_state", "CLOB", False),
    ("balance", "NUMBER(14,2)", False),
    ("customer_order_no", "NUMBER(19,0)", False),
    ("customer_total", "NUMBER(16,2)", False),
    ("month_avg", "BINARY_DOUBLE", False),
]
"""Колонки таблицы Oracle: text без длины — CLOB, uuid — VARCHAR2(36 CHAR),
timestamptz — WITH TIME ZONE, date — DATE, boolean зависит от версии."""

COMPARED: Mapping[str, tuple[str, str]] = {
    "order_id": ("{c}", "{c}"),
    "placed_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
    "month": ("to_char({c}, 'YYYY-MM-DD')", "to_char({c}, 'yyyy-mm-dd')"),
    "shipped_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
    "paid_at": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
    "lines": ("{c}", "{c}"),
    "units": ("{c}", "{c}"),
    "gross": ("{c}", "{c}"),
    "paid": ("{c}", "{c}"),
    "balance": ("{c}", "{c}"),
    "customer_order_no": ("{c}", "{c}"),
    "customer_total": ("{c}", "{c}"),
    "month_avg": ("{c}", "{c}"),
    "is_active": ("{c}::int", "case when {c} = true then 1 else 0 end"),
    "last_ip": ("host({c})", "to_char({c})"),
}
"""Как сравнить колонку: выражение postgres и выражение Oracle с одинаковым
значением; строки и CLOB — как есть."""

TEXT_ONLY = {"tags", "note", "categories", "methods", "lead_time"}
"""CLOB-колонки сравниваются через to_char: строка Oracle приходит строкой."""

ENUM_REFUSED = """
name = "pg_to_ora_enum_refused"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$raw_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "raw_orders"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
feed = "rows"
"""
"""Первая попытка LLM: enum и массив источника как есть."""

RAW_AS_CLOB = """
name = "pg_to_ora_raw_as_clob"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$raw_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "raw_orders"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
feed = "rows"
"""

REPORT = """
name = "pg_to_ora_report"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_report"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
feed = "rows"
"""
"""Ключевые поля LLM объявляет not null: их берут order by и partition by."""

REPORT_RELOAD = """
name = "pg_to_ora_report_reload"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
feed = "rows"
"""

ONE_MONTH = """
name = "pg_to_ora_one_month"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$month_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "month = to_date('$month', 'yyyy-mm-dd')" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
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
sql = '''select order_id, customer_name, tier, paid, gross, placed_at from ($plain_report_sql) r'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_mart"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { order_uid = "order_id", client = "customer_name", client_tier = "tier", paid_amount = "paid" }, column_types = { client = "VARCHAR2(60 CHAR)", client_tier = "VARCHAR2(10)" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
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
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_drift"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
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
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_drift"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
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
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_drift"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
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
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_tpl"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns}) tablespace users nologging"
chunk_bytes = 65536
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
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
after = ["insert into no_such_table values (1)"]
feed = "rows"
"""

SWAP = """
name = "pg_to_ora_swap"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
after = [
    "alter table $owner.orders_report rename to orders_report_old",
    "alter table $owner.orders_report_stage rename to orders_report",
    "drop table $owner.orders_report_old purge",
]
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
sql = '''$report_sql'''
wire = "arrow"
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "orders_report"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "nothing" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
feed = "rows"
"""

PROFILES = """
name = "pg_to_ora_profiles"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''select c.id as customer_id, json_build_object('city', c.city, 'tier', c.tier::text, 'tags', array_to_json(c.tags))::jsonb as profile from $src.customers c'''
wire = "arrow"
columns = [{ name = "customer_id", nullable = false }, { name = "profile", nullable = false }]
copy_options = { chunk_bytes = 65536 }
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "profiles"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
chunk_bytes = 65536
feed = "rows"
"""

CITY_MONTH = """
name = "ora_to_pg_city_month"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select to_char(city) as "city", month as "month", count(*) as "orders", cast(sum(gross) as number(16,2)) as "gross", cast(sum(balance) as number(16,2)) as "balance" from $owner.orders_report group by to_char(city), month'''
columns = [{ name = "city", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$src"
table_name = "city_month"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
create_table = "create table {schema_name}.{table_name} ({columns})"
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""


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


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def target(request: Any, source: PostgresSide) -> AsyncIterator[OracleSide]:
    """Схема приёмника пересоздаётся на каждый источник: сценарии опираются
    на таблицы предыдущих."""
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()

    yield side

    await side.drop()


def expected_columns(target: OracleSide) -> list[tuple[str, str, bool]]:
    boolean = "BOOLEAN"
    if target.version < BOOLEAN_SINCE:
        boolean = "NUMBER(10,0)"

    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_COLUMNS:
        columns.append((name, kind.format(boolean=boolean), not_null))

    return columns


def report_for(target: OracleSide, where: str = "") -> str:
    """Отчёт под Oracle: bool до 23 драйвер не кладёт, LLM шлёт его числом."""
    names: list[str] = []
    for name, _, _ in EXPECTED_COLUMNS:
        if name == "is_active" and target.version < BOOLEAN_SINCE:
            names.append("is_active::int as is_active")
            continue

        names.append(name)

    return f"select {', '.join(names)} from ({REPORT_SQL}) r {where}"


def month_of(target: OracleSide, month: str) -> str:
    return report_for(target, f"where r.month = date '{month}'")


def compared(target: OracleSide) -> dict[str, tuple[str, str]]:
    """Опорные выражения под версию приёмника: до 23 is_active — число."""
    templates = dict(COMPARED)
    if target.version < BOOLEAN_SINCE:
        templates["is_active"] = ("{c}::int", "{c}")

    return templates


@pytest.fixture
def dags(tmp_path: Path, source: PostgresSide, target: OracleSide) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"pg": source.profile, "ora": target.profile},
        {
            "owner": OWNER,
            "src": SRC,
            "raw_sql": RAW_SQL,
            "report_sql": report_for(target),
            "plain_report_sql": REPORT_SQL,
            "month_sql": month_of(target, MONTH),
            "month": MONTH,
        },
    )


async def _landed(
    dags: PumpDags, source: PostgresSide, target: OracleSide, spec: str
) -> str:
    """Запуск описания; отчёт приёмника — узла dst."""
    outcome = await dags.run(spec)
    report = outcome.node("dst").content
    print(
        f"\n--- {source.source.name} -> {target.source.name}: {outcome.dag} ---\n"
        f"{report}"
    )

    assert outcome.ok(), report

    return report


async def _refused(
    dags: PumpDags, source: PostgresSide, target: OracleSide, spec: str, key: str
) -> FailureResult:
    """Запуск описания, которое обязано сорваться на узле key: его отказ."""
    outcome = await dags.run(spec)
    failure = outcome.failure(key)
    print(
        f"\n--- {source.source.name} -> {target.source.name}: {outcome.dag} "
        f"refused ---\n{failure.llm_view()}"
    )

    return failure


def blanks(values: Sequence[Any]) -> list[Any]:
    """Пустая строка и NULL — одно и то же: так их хранит Oracle."""
    levelled: list[Any] = []
    for value in values:
        if value is None:
            levelled.append("")
            continue

        levelled.append(value)

    return levelled


def numbers(values: Sequence[Any]) -> list[Decimal | None]:
    parsed: list[Decimal | None] = []
    for value in values:
        if value is None:
            parsed.append(None)
            continue

        parsed.append(Decimal(str(value)))

    return parsed


async def same_content(source: PostgresSide, target: OracleSide, table: str) -> None:
    """Каждая колонка таблицы Oracle совпадает с представлением источника."""
    report = Loaded(source, SRC, "orders_report")
    landed = OraLoaded(target, table)
    templates = compared(target)
    for name, _, _ in EXPECTED_COLUMNS:
        entry = templates.get(name)
        if entry is None:
            expected = await report.texts(name, order_by="order_id")
            expression = name
            if name in TEXT_ONLY:
                expression = f"to_char({name})"

            actual = await landed.column(expression, order_by="order_id")

            assert blanks(actual) == blanks(expected), f"column {name} differs"

            continue

        pg_template, ora_template = entry
        expected = await report.scalars(pg_template.format(c=name), order_by="order_id")
        actual = await landed.column(ora_template.format(c=name), order_by="order_id")
        if ora_template == "{c}":
            assert numbers(actual) == numbers(expected), f"column {name} differs"

            continue

        assert actual == expected, f"column {name} differs"


class TestFirstAttempt:
    """LLM выгружает enum и массив как есть: у Oracle пары для них нет."""

    async def test_enum_is_refused(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        failure = await _refused(dags, source, target, ENUM_REFUSED, "dst")

        assert "column status: " in failure.llm_view()

    async def test_fallback_lands_clobs(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        report = await _landed(dags, source, target, RAW_AS_CLOB)
        landed = OraLoaded(target, "raw_orders")
        view = Loaded(source, SRC, "raw_orders")

        assert f"{ORDERS} rows loaded" in report
        assert await landed.columns() == [
            ("order_id", "NUMBER(19,0)", True),
            ("status", "CLOB", False),
            ("tier", "CLOB", False),
            ("tags", "CLOB", False),
        ]
        for name in ("status", "tier", "tags"):
            assert await landed.column(f"to_char({name})", order_by="order_id") == (
                await view.texts(name, order_by="order_id")
            ), f"column {name} differs"


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT)
        landed = OraLoaded(target, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed.columns() == expected_columns(target)
        assert await landed.count() == ORDERS
        assert await landed.scalar("count(case when paid is null then 1 end)") > 0
        assert (
            await landed.scalar("count(case when instr(note, chr(9)) > 0 then 1 end)")
            > 0
        )
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT_RELOAD)

        assert "error" not in report.split("rows loaded")[0].lower()
        assert await OraLoaded(target, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    async def test_one_month_is_replaced(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        await target.run(
            (
                f"update {OWNER}.orders_report set note = 'stale' "
                f"where month = to_date('{MONTH}', 'yyyy-mm-dd')",
            )
        )
        in_month = await Loaded(source, SRC, "orders_report").aggregate(
            f"count(case when month = date '{MONTH}' then 1 end)"
        )

        report = await _landed(dags, source, target, ONE_MONTH)

        assert in_month > 0
        assert f"{in_month} rows deleted" in report
        assert f"{in_month} rows loaded" in report
        assert (
            await landed.scalar("count(case when to_char(note) = 'stale' then 1 end)")
            == 0
        )
        assert await landed.count() == ORDERS
        await same_content(source, target, "orders_report")


class TestRenamedMart:
    """Витрина Oracle со своими именами и типами: rename_columns связывает
    колонки витрины с полями потока, column_types кладёт строки в VARCHAR2
    вместо CLOB, объявления источника снимают nullable с ключа."""

    MART: ClassVar[str] = """
        create table orders_mart (
            order_uid number(19) not null,
            client varchar2(60 char),
            client_tier varchar2(10),
            paid_amount number(18, 2),
            gross number(18, 2),
            placed_at timestamp(6) with time zone
        )"""

    async def test_missing_mart_is_refused(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        failure = await _refused(dags, source, target, MART, "dst")

        assert "table is missing" in failure.llm_view()

    async def test_mart_is_filled_by_its_own_names(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        await target.run((self.MART,))
        view = Loaded(source, SRC, "orders_report")
        mart = OraLoaded(target, "orders_mart")

        report = await _landed(dags, source, target, MART)

        assert f"{ORDERS} rows loaded" in report
        assert "is wider" in report
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
    async def test_drift_is_refused_then_backed_up(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        drift = OraLoaded(target, "orders_drift")
        await _landed(dags, source, target, DRIFT)
        await target.run((f"alter table {OWNER}.orders_drift drop column gross",))

        failure = await _refused(dags, source, target, DRIFT_CHECKED, "dst")

        assert "gross: in the stream but not in the table" in failure.llm_view()

        report = await _landed(dags, source, target, DRIFT_BACKED_UP)
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("orders_drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "orders_drift_bak_" in report
        assert len(backups) == 1
        assert await OraLoaded(target, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("gross", "NUMBER(14,2)", False) in await drift.columns()


class TestCreateTemplate:
    """Шаблон create table: tablespace и nologging; drop_and_create дважды
    подряд пересоздаёт таблицу по тому же шаблону."""

    async def test_table_gets_its_storage_options(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        for _ in range(2):
            report = await _landed(dags, source, target, TEMPLATED)

            assert f"{ORDERS} rows loaded" in report

        landed = OraLoaded(target, "orders_tpl")

        assert (
            await landed.scalar(
                f"(select logging from all_tables where owner = '{OWNER}' "
                "and table_name = 'ORDERS_TPL')"
            )
            == "NO"
        )
        assert await landed.count() == ORDERS


class TestStagingSwap:
    """Загрузка в staging и подмена витрины шагами after: rename в Oracle —
    DDL с автокоммитом, поэтому ошибка шага откатывает только строки."""

    async def test_failed_step_rolls_back_the_rows(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")

        failure = await _refused(dags, source, target, FAILED_AFTER_STEP, "dst")

        assert isinstance(failure, SqlFailureResult), failure
        assert "ORA-00942" in failure.llm_view()
        assert failure.statements[-1].status == "failed: OracleQueryError"

        tables = await landed.tables()

        assert "orders_report" in tables
        assert "orders_report_stage" in tables
        assert await OraLoaded(target, "orders_report_stage").count() == 0
        assert await landed.count() == ORDERS

    async def test_swap_replaces_the_mart(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        await target.run((f"update {OWNER}.orders_report set note = 'old'",))

        report = await _landed(dags, source, target, SWAP)
        tables = await landed.tables()

        assert f"{ORDERS} rows loaded" in report
        assert "orders_report_stage" not in tables
        assert "orders_report_old" not in tables
        assert (
            await landed.scalar("count(case when to_char(note) = 'old' then 1 end)")
            == 0
        )
        await same_content(source, target, "orders_report")


class TestDryRun:
    async def test_nothing_changes(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        columns = await landed.columns()

        report = await _landed(dags, source, target, DRY_RUN)

        assert report.startswith("0 rows loaded")
        assert await landed.columns() == columns
        assert await landed.count() == ORDERS


class TestJson:
    """Профиль клиента jsonb источника ложится CLOB и читается json-функциями
    Oracle. Источники до 9.4 json_build_object не умеют."""

    async def test_profiles_land_as_clob(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        if source.version < PG_JSON_SINCE:
            pytest.skip("json_build_object and jsonb appear in 9.4")

        report = await _landed(dags, source, target, PROFILES)
        landed = OraLoaded(target, "profiles")
        source_cities = await Loaded(source, SRC, "customers").texts("city")

        assert "rows loaded" in report
        assert await landed.columns() == [
            ("customer_id", "NUMBER(19,0)", True),
            ("profile", "CLOB", False),
        ]
        assert (
            await landed.column("json_value(profile, '$.city')", order_by="customer_id")
            == source_cities
        )


class TestBackToPostgres:
    """Обратный путь: агрегат «город × месяц» из Oracle в схему источника
    парой Oracle -> postgres; суммы совпадают с представлением."""

    async def test_city_month_comes_back(
        self, dags: PumpDags, source: PostgresSide, target: OracleSide
    ) -> None:
        await _landed(dags, source, target, CITY_MONTH)
        city_month = Loaded(source, SRC, "city_month")
        view = Loaded(source, SRC, "orders_report")

        assert await city_month.columns() == [
            ("city", "character varying(4000)", True),
            ("month", "timestamp(0) without time zone", True),
            ("orders", "numeric(38,0)", False),
            ("gross", "numeric(16,2)", False),
            ("balance", "numeric(16,2)", False),
        ]
        assert await city_month.aggregate("sum(orders)") == ORDERS
        assert await city_month.aggregate("sum(gross)") == await view.aggregate(
            "sum(gross)"
        )
        assert await city_month.aggregate("sum(balance)") == await view.aggregate(
            "sum(balance)"
        )


async def test_rows_reached_every_column(source: PostgresSide) -> None:
    view = Loaded(source, SRC, "orders_report")

    assert await view.aggregate("count(case when paid is null then 1 end)") > 0
    assert (
        await view.aggregate("count(case when payment_state = 'partial' then 1 end)")
        > 0
    )
    assert await view.aggregate("count(case when note like E'%\\t%' then 1 end)") > 0
    assert await view.count() == ORDERS
