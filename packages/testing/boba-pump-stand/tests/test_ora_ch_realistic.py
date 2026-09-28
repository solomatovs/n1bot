# ruff: noqa: S608, E501
"""Перелив отчёта по заказам из Oracle в ClickHouse парой Oracle -> ClickHouse
(ora_stream_out и ch_stream_in) запросом, каким его написал бы LLM: CTE, join и
left join с NULL, оконные функции, listagg, json_object, sys_guid.

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

from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from test_ora_pg_realistic import (
    CUSTOMERS,
    FILL_DML,
    ORDERS,
    REPORT_SQL,
    SCHEMA_DDL,
)

from boba.db.clickhouse.target import ChStreamWire, ChTableRef
from boba.db.oracle import OracleQueryError
from boba.db.oracle.target import OraTableRef
from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Leg,
    OracleSide,
    OraLoaded,
    Pumps,
    PumpStand,
)
from boba.pump_stand.names import StandNames
from boba.pump_stand.oracle import PumpUser
from boba.toolkit.contract import ColumnDeclaration
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
    InsertFull,
    InsertNothing,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
OWNER = PumpUser().name
DW = StandNames().of("shop_ora_dw")
ARRAYSIZE = 2000
CHUNK = 65536
MONTHS = 8
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

DECLARED = (
    ColumnDeclaration(name="order_id", nullable=False),
    ColumnDeclaration(name="placed_at", nullable=False),
    ColumnDeclaration(name="month", nullable=False),
)
RULES = ColumnRules(column_types={"signed_up": "DateTime64(0, 'UTC')"})

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


async def transfer(  # noqa: PLR0913
    source: OracleSide,
    target: ClickHouseSide,
    table: str,
    schema: Any,
    delete: Any,
    select: str = REPORT_SQL,
    columns: Sequence[ColumnDeclaration] = DECLARED,
    rules: ColumnRules = RULES,
    insert: Any = InsertFull(kind="full"),
    create_table: str = MERGE_TREE,
    order_by: str = "order_id",
    cluster: str = "",
    after: Sequence[str] = (),
) -> str:
    """ora_stream_out на source -> ch_stream_in на target."""
    pumps = Pumps(oracle=source.profile, clickhouse=target.profile)
    chained = await pumps.chain(
        Leg("ora_stream_out", {"sql": select, "columns": columns}),
        Leg(
            "ch_stream_in",
            {
                "database": DW,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "cluster": cluster,
                "order_by": order_by,
                "create_table": create_table,
                "after": after,
            },
        ),
    )
    print(
        f"\n--- {source.source.name} -> {target.source.name}: ora_stream_out ---\n"
        f"{chained.out_report}"
    )
    print(f"--- ch_stream_in ---\n{chained.in_report}")

    return chained.in_report


def month_of(month: str) -> str:
    return (
        f'select * from ({REPORT_SQL}) r where r."month" = '
        f"to_date('{month}', 'yyyy-mm-dd')"
    )


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
        self, source: OracleSide, target: ClickHouseSide
    ) -> None:
        with pytest.raises(OracleQueryError, match=r"signed_up.*sys_extract_utc"):
            await transfer(
                source,
                target,
                "orders_raw",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                select=(
                    f'select o.id as "order_id", c.signed_up as "signed_up" '
                    f"from {OWNER}.orders o join {OWNER}.customers c "
                    f"on c.id = o.customer_id"
                ),
                columns=(),
                rules=ColumnRules(),
            )


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, source: OracleSide, target: ClickHouseSide
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
        assert await landed.types() == EXPECTED_TYPES
        assert await landed.count() == ORDERS
        assert await landed.scalar("countIf(paid is null)") > 0
        assert await landed.scalar("countIf(position(note, '\\t') > 0)") > 0
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, source: OracleSide, target: ClickHouseSide
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
    MONTH: ClassVar[str] = "2024-03-01"

    async def test_one_month_is_replaced(
        self, source: OracleSide, target: ClickHouseSide
    ) -> None:
        landed = ChLoaded(target, "orders_report")
        await target.command(
            f"alter table {DW}.orders_report update note = 'stale' "
            f"where month = toDateTime64('{self.MONTH} 00:00:00', 0)",
            settings=SYNC,
        )
        in_month = await OraLoaded(source, "orders_report").scalar(
            f"""count(case when "month" = to_date('{self.MONTH}', 'yyyy-mm-dd') then 1 end)"""
        )

        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(
                kind="delete_where",
                where=f"month = toDateTime64('{self.MONTH} 00:00:00', 0)",
            ),
            select=month_of(self.MONTH),
        )

        assert in_month > 0
        assert f"{in_month} rows written" in report
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
    SELECT: ClassVar[str] = (
        f'select "order_id", "customer_name", "tier", "paid", "gross", "placed_at" '
        f"from ({REPORT_SQL}) r"
    )
    COLUMNS: ClassVar[tuple[ColumnDeclaration, ...]] = (
        ColumnDeclaration(name="order_id", nullable=False),
        ColumnDeclaration(name="placed_at", nullable=False),
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
        self, source: OracleSide, target: ClickHouseSide
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
        self, source: OracleSide, target: ClickHouseSide
    ) -> None:
        await target.command(f"drop table if exists {DW}.orders_mart")
        await target.command(self.MART)
        view = OraLoaded(source, "orders_report")
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
        self, source: OracleSide, target: ClickHouseSide
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
            f"alter table {DW}.orders_drift modify column gross Nullable(Decimal(12, 1))",
            settings=SYNC,
        )

        with pytest.raises(TransferError, match=r"gross: .*truncates"):
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
        assert ("gross", "Nullable(Decimal(14, 2))") in await drift.types()


class TestCreateTemplate:
    async def test_partitioned_by_month(
        self, source: OracleSide, target: ClickHouseSide
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
        self, source: OracleSide, target: ClickHouseSide
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
    SWAP: ClassVar[tuple[str, ...]] = (
        f"exchange tables {DW}.orders_report and {DW}.orders_report_stage",
        f"drop table {DW}.orders_report_stage",
    )

    async def test_swap_replaces_the_mart(
        self, source: OracleSide, target: ClickHouseSide
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
    async def test_nothing_changes(
        self, source: OracleSide, target: ClickHouseSide
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


class TestBackToOracle:
    """Обратный путь: агрегат «город × месяц» из ClickHouse в Oracle парой
    ClickHouse -> Oracle по arrow; суммы совпадают с отчётом источника."""

    AGGREGATE: ClassVar[str] = (
        f"select city, month, toInt64(count()) as orders, "
        f"toDecimal64(sum(gross), 2) as gross, toDecimal64(sum(balance), 2) as balance "
        f"from {DW}.orders_report group by city, month "
        f"settings output_format_arrow_string_as_string = 1"
    )

    async def test_city_month_comes_back(
        self, source: OracleSide, target: ClickHouseSide
    ) -> None:
        pumps = Pumps(oracle=source.profile, clickhouse=target.profile)
        chained = await pumps.chain(
            Leg(
                "ch_stream_out",
                {
                    "sql": self.AGGREGATE,
                    "wire": ChStreamWire.ARROW,
                    "chunk_bytes": CHUNK,
                },
            ),
            Leg(
                "ora_stream_in",
                {
                    "schema_name": OWNER,
                    "table_name": "city_month",
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "create_table": OraTableRef.CREATE_TABLE,
                    "chunk_bytes": CHUNK,
                },
            ),
        )
        print(f"\n--- {target.source.name} -> {source.source.name} ---")
        print(chained.in_report)
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
