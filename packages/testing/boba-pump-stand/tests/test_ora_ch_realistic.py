# ruff: noqa: S608, E501
"""Перелив отчёта по заказам из Oracle в ClickHouse парой Oracle -> ClickHouse
(ora_stream_out и ch_stream_in) запросом, каким его написал бы LLM: CTE, join и
left join с NULL, оконные функции, listagg, json_object, sys_guid.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Источник — каждый Oracle стенда, приёмник — каждый ClickHouse стенда.
Сценарии идут по порядку на каждой паре и опираются на таблицы предыдущих:
    - первая попытка LLM с TIMESTAMP WITH TIME ZONE: отказ источника с
      подсказкой, затем sys_extract_utc и зонный тип через column_types;
    - первая загрузка отчёта с типами ClickHouse по контракту, повторная с
      проверкой схемы;
    - инкремент одного месяца через delete_where и двойник;
    - витрина приёмника со своими именами через rename_columns и column_types;
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table с partition by, ReplicatedMergeTree on cluster там,
      где есть Keeper;
    - подмена витрины шагом after через exchange tables;
    - сухой прогон do_nothing + insert nothing;
    - обратный путь: агрегат «город × месяц» из ClickHouse в Oracle.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from test_ora_pg_realistic import (
    CUSTOMERS,
    FILL_DML,
    ORDERS,
    REPORT_SQL,
    SCHEMA_DDL,
)

from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    OracleSide,
    OraLoaded,
    PumpDags,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.stand.names import StandNames
from boba.toolkit.result import FailureResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
OWNER = PumpUser().name
DW = StandNames().of("shop_ora_dw")
ARRAYSIZE = 2000
MONTHS = 8
KEEPER_CLUSTER = "stand"
MONTH = "2024-03-01"
SYNC = {"mutations_sync": 2}

MONTH_SQL = f"""select * from ({REPORT_SQL}) r where r."month" = to_date('{MONTH}', 'yyyy-mm-dd')"""

EXPECTED_TYPES = [
    ("order_id", "Int64"),
    ("status", "String"),
    ("customer_uid", "Nullable(String)"),
    ("customer_name", "String"),
    ("email", "Nullable(String)"),
    ("tier", "String"),
    ("city", "String"),
    ("tags", "String"),
    ("last_ip", "Nullable(String)"),
    ("is_active", "Int64"),
    ("signed_up", "Nullable(DateTime64(0, 'UTC'))"),
    ("placed_at", "DateTime64(6)"),
    ("month", "DateTime64(0)"),
    ("shipped_at", "Nullable(DateTime64(6))"),
    ("lead_time_s", "Nullable(Decimal(38, 0))"),
    ("note", "Nullable(String)"),
    ("lines", "Nullable(Decimal(38, 0))"),
    ("units", "Nullable(Decimal(38, 0))"),
    ("gross", "Nullable(Decimal(14, 2))"),
    ("categories", "Nullable(String)"),
    ("lines_json", "Nullable(String)"),
    ("paid", "Nullable(Decimal(14, 2))"),
    ("paid_at", "Nullable(DateTime64(6))"),
    ("methods", "Nullable(String)"),
    ("payment_state", "Nullable(String)"),
    ("balance", "Nullable(Decimal(14, 2))"),
    ("customer_order_no", "Nullable(Decimal(38, 0))"),
    ("customer_total", "Nullable(Decimal(16, 2))"),
    ("month_avg", "Nullable(Decimal(18, 6))"),
]
"""Типы ClickHouse по контракту Oracle: not null таблиц источника
доходит до колонок, выражения — Nullable."""

COMPARED: Mapping[str, tuple[str, str]] = {
    "order_id": ("{c}", "{c}"),
    "is_active": ("{c}", "{c}"),
    "signed_up": ("toString({c})", "to_char({c}, 'yyyy-mm-dd hh24:mi:ss')"),
    "placed_at": ("toString({c})", "to_char({c}, 'yyyy-mm-dd hh24:mi:ss.ff6')"),
    "month": ("toString({c})", "to_char({c}, 'yyyy-mm-dd hh24:mi:ss')"),
    "shipped_at": ("toString({c})", "to_char({c}, 'yyyy-mm-dd hh24:mi:ss.ff6')"),
    "paid_at": ("toString({c})", "to_char({c}, 'yyyy-mm-dd hh24:mi:ss.ff6')"),
    "lead_time_s": ("{c}", "{c}"),
    "lines": ("{c}", "{c}"),
    "units": ("{c}", "{c}"),
    "gross": ("{c}", "{c}"),
    "paid": ("{c}", "{c}"),
    "balance": ("{c}", "{c}"),
    "customer_order_no": ("{c}", "{c}"),
    "customer_total": ("{c}", "{c}"),
    "month_avg": ("{c}", "{c}"),
}
"""Как сравнить колонку: выражение ClickHouse и выражение Oracle с одинаковым
значением; строки сравниваются как есть."""

ZONED_REFUSED = """
name = "ora_to_ch_zoned_refused"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select o.id as "order_id", c.signed_up as "signed_up" from $owner.orders o join $owner.customers c on c.id = o.customer_id'''
columns = []
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_raw"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

REPORT = """
name = "ora_to_ch_report"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

REPORT_RELOAD = """
name = "ora_to_ch_report_reload"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

ONE_MONTH = """
name = "ora_to_ch_one_month"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$month_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "month = toDateTime64('$month 00:00:00', 0)" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

MART = """
name = "ora_to_ch_mart"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select "order_id", "customer_name", "tier", "paid", "gross", "placed_at" from ($report_sql) r'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_mart"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { order_uid = "order_id", client = "customer_name", client_tier = "tier", paid_amount = "paid" }, column_types = { client_tier = "LowCardinality(String)" } }
cluster = ""
order_by = "order_uid"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

DRIFT = """
name = "ora_to_ch_drift"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

DRIFT_CHECKED = """
name = "ora_to_ch_drift_checked"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

DRIFT_BACKED_UP = """
name = "ora_to_ch_drift_backed_up"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_drift"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

PARTITIONED = """
name = "ora_to_ch_partitioned"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_tpl"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "(month, order_id)"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree partition by toYYYYMM(month) order by {order_by}"
feed = "rows"
"""

REPLICATED = """
name = "ora_to_ch_replicated"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_replicated"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = "$keeper_cluster"
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = ReplicatedMergeTree order by {order_by}"
feed = "rows"
"""

SWAP = """
name = "ora_to_ch_swap"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_report_stage"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
after = [
    "exchange tables $dw.orders_report and $dw.orders_report_stage",
    "drop table $dw.orders_report_stage",
]
feed = "rows"
"""

DRY_RUN = """
name = "ora_to_ch_dry_run"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''$report_sql'''
columns = [{ name = "order_id", nullable = false }, { name = "placed_at", nullable = false }, { name = "month", nullable = false }]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "orders_report"
schema_strategy = { kind = "do_nothing" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "nothing" }
rules = { column_types = { signed_up = "DateTime64(0, 'UTC')" } }
cluster = ""
order_by = "order_id"
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

CITY_MONTH = """
name = "ch_to_ora_city_month"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select city, month, toInt64(count()) as orders, toDecimal64(sum(gross), 2) as gross, toDecimal64(sum(balance), 2) as balance from $dw.orders_report group by city, month settings output_format_arrow_string_as_string = 1"
wire = "arrow"
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$owner"
table_name = "city_month"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {schema_name}.{table_name} ({columns})"
feed = "rows"
"""


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def source(request: Any) -> AsyncIterator[OracleSide]:
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    await side.run(SCHEMA_DDL)
    await side.run(FILL_DML)
    await side.run((f"create table orders_report as {REPORT_SQL}",))

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def target(request: Any, source: OracleSide) -> AsyncIterator[ClickHouseSide]:
    """База приёмника пересоздаётся на каждый Oracle: сценарии опираются на
    таблицы предыдущих."""
    side = ClickHouseSide(request.param, DW)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


@pytest.fixture
def dags(tmp_path: Path, source: OracleSide, target: ClickHouseSide) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"ora": source.profile, "ch": target.profile},
        {
            "owner": OWNER,
            "dw": DW,
            "report_sql": REPORT_SQL,
            "month_sql": MONTH_SQL,
            "month": MONTH,
            "keeper_cluster": KEEPER_CLUSTER,
        },
    )


async def _landed(
    dags: PumpDags, source: OracleSide, target: ClickHouseSide, spec: str
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
    dags: PumpDags, source: OracleSide, target: ClickHouseSide, spec: str, key: str
) -> FailureResult:
    """Запуск описания, которое обязано сорваться на узле key: его отказ."""
    outcome = await dags.run(spec)
    failure = outcome.failure(key)
    print(
        f"\n--- {source.source.name} -> {target.source.name}: {outcome.dag} "
        f"refused ---\n{failure.llm_view()}"
    )

    return failure


def numbers(values: Sequence[Any]) -> list[Decimal | None]:
    parsed: list[Decimal | None] = []
    for value in values:
        if value is None:
            parsed.append(None)
            continue

        parsed.append(Decimal(str(value)))

    return parsed


async def same_content(source: OracleSide, target: ClickHouseSide, table: str) -> None:
    """Каждая колонка таблицы ClickHouse совпадает с отчётом, снятым в
    таблицу Oracle тем же запросом."""
    report = OraLoaded(source, "orders_report")
    landed = ChLoaded(target, table)
    for name, _ in EXPECTED_TYPES:
        entry = COMPARED.get(name)
        if entry is None:
            actual = await landed.column(name, order_by="order_id")
            expected = await report.column(f'"{name}"', order_by='"order_id"')

            assert actual == expected, f"column {name} differs"

            continue

        ch_template, ora_template = entry
        actual = await landed.column(ch_template.format(c=name), order_by="order_id")
        expected = await report.column(
            ora_template.format(c=f'"{name}"'), order_by='"order_id"'
        )
        if ora_template == "{c}":
            assert numbers(actual) == numbers(expected), f"column {name} differs"

            continue

        assert actual == expected, f"column {name} differs"


class TestFirstAttempt:
    async def test_zoned_timestamp_is_refused(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        failure = await _refused(dags, source, target, ZONED_REFUSED, "src")

        assert failure.error_kind == "OracleQueryError"
        assert re.search(r"signed_up.*sys_extract_utc", failure.llm_view())


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT)
        landed = ChLoaded(target, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed.types() == EXPECTED_TYPES
        assert await landed.count() == ORDERS
        assert await landed.scalar("countIf(paid is null)") > 0
        assert await landed.scalar("countIf(position(note, '\\t') > 0)") > 0
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        report = await _landed(dags, source, target, REPORT_RELOAD)

        assert "error" not in report.split("rows loaded")[0].lower()
        assert await ChLoaded(target, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    async def test_one_month_is_replaced(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        await target.command(
            f"alter table {DW}.orders_report update note = 'stale' "
            f"where month = toDateTime64('{MONTH} 00:00:00', 0)",
            settings=SYNC,
        )
        in_month = await OraLoaded(source, "orders_report").scalar(
            f"""count(case when "month" = to_date('{MONTH}', 'yyyy-mm-dd') then 1 end)"""
        )

        report = await _landed(dags, source, target, ONE_MONTH)

        assert in_month > 0
        assert f"{in_month} rows loaded" in report
        assert await landed.scalar("countIf(note = 'stale')") == 0
        assert await landed.count() == ORDERS
        await same_content(source, target, "orders_report")


class TestRenamedMart:
    MART: ClassVar[str] = f"""
        create table {DW}.orders_mart (
            order_uid Int64,
            client String,
            client_tier LowCardinality(String),
            paid_amount Nullable(Decimal(18, 2)),
            gross Nullable(Decimal(18, 2)),
            placed_at DateTime64(6)
        ) engine = MergeTree order by order_uid"""

    async def test_missing_mart_is_refused(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        failure = await _refused(dags, source, target, MART, "dst")

        assert "table is missing" in failure.llm_view()

    async def test_mart_is_filled_by_its_own_names(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        await target.command(f"drop table if exists {DW}.orders_mart")
        await target.command(self.MART)
        view = OraLoaded(source, "orders_report")
        mart = ChLoaded(target, "orders_mart")

        report = await _landed(dags, source, target, MART)

        assert f"{ORDERS} rows loaded" in report
        assert "is wider" in report
        assert await mart.count() == ORDERS
        assert await mart.column("client", order_by="order_uid") == (
            await view.column('"customer_name"', order_by='"order_id"')
        )
        assert await mart.column("client_tier", order_by="order_uid") == (
            await view.column('"tier"', order_by='"order_id"')
        )
        assert numbers([await mart.scalar("sum(paid_amount)")]) == numbers(
            [await view.scalar('sum("paid")')]
        )


class TestSchemaDrift:
    async def test_drift_is_refused_then_backed_up(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        drift = ChLoaded(target, "orders_drift")
        await _landed(dags, source, target, DRIFT)
        await target.command(
            f"alter table {DW}.orders_drift modify column gross Nullable(Decimal(12, 1))",
            settings=SYNC,
        )

        failure = await _refused(dags, source, target, DRIFT_CHECKED, "dst")

        assert re.search(r"gross: .*truncates", failure.llm_view())

        report = await _landed(dags, source, target, DRIFT_BACKED_UP)
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("orders_drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "orders_drift_bak_" in report
        assert len(backups) == 1
        assert await ChLoaded(target, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("gross", "Nullable(Decimal(14, 2))") in await drift.types()


class TestCreateTemplate:
    async def test_partitioned_by_month(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        for _ in range(2):
            report = await _landed(dags, source, target, PARTITIONED)

            assert f"{ORDERS} rows loaded" in report

        landed = ChLoaded(target, "orders_tpl")

        assert await landed.sorting_key() == "month, order_id"
        assert await landed.scalar("uniqExact(_partition_id)") == MONTHS
        assert await landed.count() == ORDERS

    async def test_replicated_on_cluster(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_replicated")
        if KEEPER_CLUSTER not in await landed.clusters():
            pytest.skip("the server has no Keeper cluster")

        report = await _landed(dags, source, target, REPLICATED)

        assert f"{ORDERS} rows loaded" in report
        assert await landed.engine() == "ReplicatedMergeTree"
        assert await landed.count() == ORDERS


class TestAfterSwap:
    async def test_swap_replaces_the_mart(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        await target.command(
            f"alter table {DW}.orders_report update note = 'old' where 1",
            settings=SYNC,
        )

        report = await _landed(dags, source, target, SWAP)

        assert f"{ORDERS} rows loaded" in report
        assert "orders_report_stage" not in await landed.tables()
        assert await landed.scalar("countIf(note = 'old')") == 0
        await same_content(source, target, "orders_report")


class TestDryRun:
    async def test_nothing_changes(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        types = await landed.types()

        report = await _landed(dags, source, target, DRY_RUN)

        assert report.startswith("0 rows loaded")
        assert await landed.types() == types
        assert await landed.count() == ORDERS


class TestBackToOracle:
    """Обратный путь: агрегат «город × месяц» из ClickHouse в Oracle парой
    ClickHouse -> Oracle по arrow; суммы совпадают с отчётом источника."""

    async def test_city_month_comes_back(
        self, dags: PumpDags, source: OracleSide, target: ClickHouseSide
    ) -> None:
        await _landed(dags, source, target, CITY_MONTH)
        city_month = OraLoaded(source, "city_month")
        view = OraLoaded(source, "orders_report")

        assert await city_month.columns() == [
            ("city", "CLOB", False),
            ("month", "TIMESTAMP(0) WITH TIME ZONE", True),
            ("orders", "NUMBER(19,0)", True),
            ("gross", "NUMBER(18,2)", False),
            ("balance", "NUMBER(18,2)", False),
        ]
        assert await city_month.scalar("sum(orders)") == ORDERS
        assert await city_month.scalar("sum(gross)") == await view.scalar(
            'sum("gross")'
        )
        assert await city_month.scalar("sum(balance)") == await view.scalar(
            'sum("balance")'
        )
        assert await city_month.count() == await view.scalar(
            'count(distinct "city" || to_char("month", \'yyyy-mm\'))'
        )


async def test_rows_reached_every_column(source: OracleSide) -> None:
    view = OraLoaded(source, "orders_report")

    assert await view.scalar('count(case when "paid" is null then 1 end)') > 0
    assert (
        await view.scalar("""count(case when "payment_state" = 'partial' then 1 end)""")
        > 0
    )
    assert await view.scalar('count(case when "last_ip" is null then 1 end)') > 0
    assert await view.count() == ORDERS
    assert await OraLoaded(source, "customers").count() == CUSTOMERS
