# ruff: noqa: S608
"""Перелив отчётов магазина из ClickHouse в Oracle парой ClickHouse -> Oracle
(ch_stream_out с wire = arrow и ora_stream_in) запросами, какими их написал бы
LLM: CTE, join и left join с join_use_nulls, оконные функции, агрегаты в
JSON, Enum, LowCardinality, UUID, IPv4, Decimal, DateTime64 в UTC.

Источник — каждый ClickHouse стенда, приёмник — каждый Oracle стенда.
Сценарии идут по порядку на каждой паре и опираются на таблицы предыдущих:
    - первая попытка LLM с Date и DateTime как есть: в Arrow они уходят
      числами, и приёмник создаёт NUMBER; запрос переписан с toDate32 и
      toDateTime64;
    - загрузка отчёта с типами Oracle по контракту, повторная с проверкой
      схемы;
    - инкремент одного месяца через delete_where;
    - витрина приёмника со своими именами через rename_columns и column_types
      (VARCHAR2 вместо CLOB);
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table с tablespace и nologging;
    - подмена витрины шагами after через rename, откат строк при ошибке шага;
    - сухой прогон do_nothing + insert nothing;
    - обратный путь: агрегат «город × месяц» из Oracle в ClickHouse.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from test_ch_pg_realistic import (
    FILL_DML,
    JOIN_NULLS,
    ORDERS,
    REPORT_BODY,
    SCHEMA_DDL,
)
from test_ch_pg_realistic import SRC as CH_SRC

from boba.db.clickhouse.target import ChStreamWire
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
ARRAYSIZE = 2000
CHUNK = 65536
BOOLEAN_SINCE = 23
STRING_AS_STRING = "output_format_arrow_string_as_string = 1"
REPORT_SETTINGS = f"{JOIN_NULLS}, {STRING_AS_STRING}"
"""Настройки отчёта ставятся у самого внешнего запроса: ClickHouse до 24 не
доносит settings подзапроса до формата ответа, и строки уходят binary."""
MERGE_TREE = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree order by {order_by}"
)

REPORT_COLUMNS = """
select order_id,
       toString(status)                     as status,
       toString(customer_uid)               as customer_uid,
       customer_name,
       email,
       toString(tier)                       as tier,
       toString(city)                       as city,
       tags,
       toString(last_ip)                    as last_ip,
       {is_active},
       toDateTime64(signed_up, 0, 'UTC')    as signed_up,
       placed_at,
       toDate32(month)                      as month,
       shipped_at,
       lead_time_s,
       note,
       lines,
       units,
       gross,
       categories,
       lines_json,
       paid,
       paid_at,
       methods,
       payment_state,
       balance,
       customer_order_no,
       customer_total,
       month_avg
from ({body}) r"""
"""Отчёт под Oracle: Enum и LowCardinality — строкой, IPv4 и UUID — текстом,
Date и DateTime — настоящими моментами (в Arrow они уходят числами), Bool
до 23 — числом: приёмник значения не переписывает."""


def report_query(target: OracleSide) -> str:
    """Отчёт без settings: для вложения в другой запрос."""
    is_active = "is_active"
    if target.version < BOOLEAN_SINCE:
        is_active = "toUInt8(is_active) as is_active"

    return REPORT_COLUMNS.format(is_active=is_active, body=REPORT_BODY)


def report_sql(clickhouse: ClickHouseSide, target: OracleSide) -> str:
    return f"{report_query(target)}\n{REPORT_SETTINGS}"


def native_boolean(source: ClickHouseSide, target: OracleSide) -> bool:
    """Bool ложится в BOOLEAN: нужен Oracle 23+ и ClickHouse 23+ — 22.12
    отдаёт Bool в Arrow числом uint8."""
    if target.version < BOOLEAN_SINCE:
        return False

    return source.major >= BOOLEAN_SINCE


def compared(source: ClickHouseSide, target: OracleSide) -> dict[str, tuple[str, str]]:
    templates = dict(COMPARED)
    if not native_boolean(source, target):
        templates["is_active"] = ("toUInt8({c})", "{c}")

    return templates


DECLARED = (
    ColumnDeclaration(name="order_id", nullable=False),
    ColumnDeclaration(name="placed_at", nullable=False),
    ColumnDeclaration(name="month", nullable=False),
)

EXPECTED_COLUMNS = [
    ("order_id", "NUMBER(20,0)", True),
    ("status", "CLOB", False),
    ("customer_uid", "CLOB", False),
    ("customer_name", "CLOB", False),
    ("email", "CLOB", False),
    ("tier", "CLOB", False),
    ("city", "CLOB", False),
    ("tags", "CLOB", False),
    ("last_ip", "CLOB", False),
    ("is_active", "{boolean}", True),
    ("signed_up", "TIMESTAMP(0) WITH TIME ZONE", True),
    ("placed_at", "TIMESTAMP(3) WITH TIME ZONE", True),
    ("month", "DATE", True),
    ("shipped_at", "TIMESTAMP(3) WITH TIME ZONE", False),
    ("lead_time_s", "NUMBER(19,0)", False),
    ("note", "CLOB", False),
    ("lines", "NUMBER(20,0)", True),
    ("units", "NUMBER(20,0)", True),
    ("gross", "NUMBER(18,2)", True),
    ("categories", "CLOB", False),
    ("lines_json", "CLOB", False),
    ("paid", "NUMBER(38,2)", False),
    ("paid_at", "TIMESTAMP(3) WITH TIME ZONE", False),
    ("methods", "CLOB", False),
    ("payment_state", "CLOB", False),
    ("balance", "NUMBER(18,2)", True),
    ("customer_order_no", "NUMBER(20,0)", True),
    ("customer_total", "NUMBER(38,2)", True),
    ("month_avg", "BINARY_DOUBLE", True),
]
"""Колонки таблицы Oracle: UInt64 — NUMBER(20), String — CLOB и всегда
nullable (пустая строка в Oracle — NULL), Decimal — NUMBER(p, s), DateTime64
в UTC — WITH TIME ZONE, Date32 — DATE, Bool и UUID зависят от версий
серверов."""

COMPARED: Mapping[str, tuple[str, str]] = {
    "order_id": ("{c}", "{c}"),
    "is_active": ("toUInt8({c})", "case when {c} = true then 1 else 0 end"),
    "signed_up": (
        "toString({c})",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss')",
    ),
    "placed_at": (
        "toString({c})",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss.ff3')",
    ),
    "month": ("toString({c})", "to_char({c}, 'yyyy-mm-dd')"),
    "shipped_at": (
        "toString({c})",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss.ff3')",
    ),
    "paid_at": (
        "toString({c})",
        "to_char(sys_extract_utc({c}), 'yyyy-mm-dd hh24:mi:ss.ff3')",
    ),
    "lead_time_s": ("{c}", "{c}"),
    "lines": ("{c}", "{c}"),
    "units": ("{c}", "{c}"),
    "gross": ("{c}", "{c}"),
    "paid": ("{c}", "{c}"),
    "balance": ("{c}", "{c}"),
    "customer_order_no": ("{c}", "{c}"),
    "customer_total": ("{c}", "{c}"),
    "customer_uid": ("toString({c})", "to_char({c})"),
    "last_ip": ("toString({c})", "to_char({c})"),
}
"""Как сравнить колонку: выражение ClickHouse и выражение Oracle с одинаковым
значением; CLOB — через to_char, числа — значением."""

FLOAT_COLUMNS = {"month_avg": ("{c}", "{c}")}
"""Среднее окна ClickHouse считает заново при выгрузке, порядок сложения
float меняется: сравнение с допуском в последних битах."""


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def source(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_SRC)
    await side.connect()
    await side.recreate_database()
    for statement in [*SCHEMA_DDL, *FILL_DML]:
        await side.command(statement)

    await side.command(
        f"create table {CH_SRC}.orders_report engine = MergeTree order by order_id "
        f"as {REPORT_BODY}",
        settings={"join_use_nulls": 1},
    )

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def target(request: Any, source: ClickHouseSide) -> AsyncIterator[OracleSide]:
    """Схема приёмника пересоздаётся на каждый ClickHouse: сценарии опираются
    на таблицы предыдущих."""
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()

    yield side

    await side.drop()


def expected_columns(
    source: ClickHouseSide, target: OracleSide
) -> list[tuple[str, str, bool]]:
    boolean = "BOOLEAN"
    if not native_boolean(source, target):
        boolean = "NUMBER(3,0)"

    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_COLUMNS:
        columns.append((name, kind.format(boolean=boolean), not_null))

    return columns


async def transfer(  # noqa: PLR0913
    source: ClickHouseSide,
    target: OracleSide,
    table: str,
    schema: Any,
    delete: Any,
    select: str,
    columns: Sequence[ColumnDeclaration] = DECLARED,
    rules: ColumnRules = ColumnRules(),
    insert: Any = InsertFull(kind="full"),
    create_table: str = OraTableRef.CREATE_TABLE,
    after: Sequence[str] = (),
) -> str:
    """ch_stream_out (arrow) на source -> ora_stream_in на target."""
    pumps = Pumps(clickhouse=source.profile, oracle=target.profile)
    chained = await pumps.chain(
        Leg(
            "ch_stream_out",
            {
                "sql": select,
                "wire": ChStreamWire.ARROW,
                "columns": columns,
                "chunk_bytes": CHUNK,
            },
        ),
        Leg(
            "ora_stream_in",
            {
                "schema_name": OWNER,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "create_table": create_table,
                "chunk_bytes": CHUNK,
                "after": after,
            },
        ),
    )
    print(
        f"\n--- {source.source.name} -> {target.source.name}: "
        f"ch_stream_out (arrow) ---\n"
        f"{chained.out_report}"
    )
    print(f"--- ora_stream_in ---\n{chained.in_report}")

    return chained.in_report


def month_of(source: ClickHouseSide, target: OracleSide, month: str) -> str:
    report = report_query(target)

    return (
        f"select * from ({report}) m where m.month = toDate32('{month}') "
        f"{REPORT_SETTINGS}"
    )


def numbers(values: Sequence[Any]) -> list[Decimal | None]:
    parsed: list[Decimal | None] = []
    for value in values:
        if value is None:
            parsed.append(None)
            continue

        parsed.append(Decimal(str(value)))

    return parsed


async def same_content(source: ClickHouseSide, target: OracleSide, table: str) -> None:
    """Каждая колонка таблицы Oracle совпадает с отчётом ClickHouse."""
    report = ChLoaded(source, "orders_report")
    landed = OraLoaded(target, table)
    templates = compared(source, target)
    for name, _, _ in EXPECTED_COLUMNS:
        ch_template, ora_template = templates.get(
            name, FLOAT_COLUMNS.get(name, ("toString({c})", "to_char({c})"))
        )
        expected = await report.column(ch_template.format(c=name), order_by="order_id")
        actual = await landed.column(ora_template.format(c=name), order_by="order_id")
        if name in FLOAT_COLUMNS:
            for got, want in zip(actual, expected, strict=True):
                assert abs(got - want) <= abs(want) * 1e-12, f"column {name} differs"

            continue

        if ch_template == "{c}":
            assert numbers(actual) == numbers(expected), f"column {name} differs"

            continue

        assert blanks(actual) == blanks(expected), f"column {name} differs"


def blanks(values: Sequence[Any]) -> list[Any]:
    """Пустая строка и NULL — одно и то же: так их хранит Oracle."""
    levelled: list[Any] = []
    for value in values:
        if value is None:
            levelled.append("")
            continue

        levelled.append(value)

    return levelled


class TestFirstAttempt:
    """Date и DateTime как есть уходят в Arrow числами: приёмник по контракту
    создаёт NUMBER, и это видно по колонкам. LLM переписывает запрос."""

    async def test_dates_become_numbers(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_raw",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=(
                f"select order_id, signed_up, month from ({REPORT_BODY}) r "
                f"limit 10 {JOIN_NULLS}"
            ),
            columns=(ColumnDeclaration(name="order_id", nullable=False),),
        )
        columns: dict[str, str] = {}
        for name, kind, _ in await OraLoaded(target, "orders_raw").columns():
            columns[name] = kind

        assert "rows loaded" in report
        assert columns["signed_up"] == "NUMBER(10,0)"
        if source.major < 26:
            assert columns["month"] == "NUMBER(5,0)"
        else:
            assert columns["month"] == "DATE"


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=report_sql(source, target),
        )
        landed = OraLoaded(target, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert await landed.columns() == expected_columns(source, target)
        assert await landed.count() == ORDERS
        assert await landed.scalar("count(case when paid is null then 1 end)") > 0
        assert (
            await landed.scalar("count(case when instr(note, chr(9)) > 0 then 1 end)")
            > 0
        )
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
            select=report_sql(source, target),
        )

        assert "error" not in report.split("rows loaded")[0].lower()
        assert await OraLoaded(target, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    MONTH: ClassVar[str] = "2024-03-01"

    async def test_one_month_is_replaced(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        await target.run(
            (
                f"update {OWNER}.orders_report set note = 'stale' "
                f"where month = to_date('{self.MONTH}', 'yyyy-mm-dd')",
            )
        )
        in_month = await ChLoaded(source, "orders_report").scalar(
            f"countIf(month = toDate('{self.MONTH}'))"
        )

        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(
                kind="delete_where",
                where=f"month = to_date('{self.MONTH}', 'yyyy-mm-dd')",
            ),
            select=month_of(source, target, self.MONTH),
        )

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
    MART: ClassVar[str] = """
        create table orders_mart (
            order_uid number(20) not null,
            client varchar2(60 char),
            client_tier varchar2(10),
            paid_amount number(38, 2),
            gross number(18, 2),
            placed_at timestamp(3) with time zone
        )"""
    RULES: ClassVar[ColumnRules] = ColumnRules(
        rename_columns={
            "order_uid": "order_id",
            "client": "customer_name",
            "client_tier": "tier",
            "paid_amount": "paid",
        },
        column_types={"client": "VARCHAR2(60 CHAR)", "client_tier": "VARCHAR2(10)"},
    )
    COLUMNS: ClassVar[tuple[ColumnDeclaration, ...]] = (
        ColumnDeclaration(name="order_id", nullable=False),
        ColumnDeclaration(name="placed_at", nullable=False),
    )

    def select(self, source: ClickHouseSide, target: OracleSide) -> str:
        return (
            f"select order_id, customer_name, tier, paid, gross, placed_at "
            f"from ({report_query(target)}) m {REPORT_SETTINGS}"
        )

    async def test_missing_mart_is_refused(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        with pytest.raises(TransferError, match="table is missing"):
            await transfer(
                source,
                target,
                "orders_mart",
                ErrorIfNotExists(kind="error_if_not_exists"),
                DeleteNothing(kind="nothing"),
                select=self.select(source, target),
                columns=self.COLUMNS,
                rules=self.RULES,
            )

    async def test_mart_is_filled_by_its_own_names(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        await target.run((self.MART,))
        view = ChLoaded(source, "orders_report")
        mart = OraLoaded(target, "orders_mart")

        report = await transfer(
            source,
            target,
            "orders_mart",
            ErrorIfNotExists(kind="error_if_not_exists"),
            DeleteNothing(kind="nothing"),
            select=self.select(source, target),
            columns=self.COLUMNS,
            rules=self.RULES,
        )

        assert f"{ORDERS} rows loaded" in report
        assert await mart.count() == ORDERS
        assert await mart.column("client", order_by="order_uid") == (
            await view.column("customer_name", order_by="order_id")
        )
        assert await mart.column("client_tier", order_by="order_uid") == (
            await view.column("toString(tier)", order_by="order_id")
        )
        assert numbers([await mart.scalar("sum(paid_amount)")]) == numbers(
            [await view.scalar("sum(paid)")]
        )


class TestSchemaDrift:
    async def test_drift_is_refused_then_backed_up(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        drift = OraLoaded(target, "orders_drift")
        await transfer(
            source,
            target,
            "orders_drift",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=report_sql(source, target),
        )
        await target.run((f"alter table {OWNER}.orders_drift drop column gross",))

        with pytest.raises(
            TransferError, match="gross: in the stream but not in the table"
        ):
            await transfer(
                source,
                target,
                "orders_drift",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                DeleteTruncate(kind="truncate"),
                select=report_sql(source, target),
            )

        report = await transfer(
            source,
            target,
            "orders_drift",
            BackupAndCreateIfSchemaChanged(kind="backup_and_create_if_schema_changed"),
            DeleteNothing(kind="nothing"),
            select=report_sql(source, target),
        )
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("orders_drift_bak_"):
                backups.append(name)

        assert "saved as" in report
        assert "orders_drift_bak_" in report
        assert len(backups) == 1
        assert await OraLoaded(target, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("gross", "NUMBER(18,2)", True) in await drift.columns()


class TestCreateTemplate:
    TEMPLATE: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) tablespace users nologging"
    )

    async def test_table_gets_its_storage_options(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        for _ in range(2):
            report = await transfer(
                source,
                target,
                "orders_tpl",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                select=report_sql(source, target),
                create_table=self.TEMPLATE,
            )

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
    SWAP: ClassVar[tuple[str, ...]] = (
        f"alter table {OWNER}.orders_report rename to orders_report_old",
        f"alter table {OWNER}.orders_report_stage rename to orders_report",
        f"drop table {OWNER}.orders_report_old purge",
    )

    async def test_failed_step_rolls_back_the_rows(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")

        with pytest.raises(OracleQueryError, match="ORA-00942"):
            await transfer(
                source,
                target,
                "orders_report_stage",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                select=report_sql(source, target),
                after=("insert into no_such_table values (1)",),
            )

        assert await OraLoaded(target, "orders_report_stage").count() == 0
        assert await landed.count() == ORDERS

    async def test_swap_replaces_the_mart(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        await target.run((f"update {OWNER}.orders_report set note = 'old'",))

        report = await transfer(
            source,
            target,
            "orders_report_stage",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=report_sql(source, target),
            after=self.SWAP,
        )
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
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        columns = await landed.columns()

        report = await transfer(
            source,
            target,
            "orders_report",
            DoNothing(kind="do_nothing"),
            DeleteNothing(kind="nothing"),
            select=report_sql(source, target),
            insert=InsertNothing(kind="nothing"),
        )

        assert report.startswith("0 rows loaded")
        assert await landed.columns() == columns
        assert await landed.count() == ORDERS


class TestBackToClickHouse:
    """Обратный путь: агрегат «город × месяц» из Oracle в ClickHouse парой
    Oracle -> ClickHouse; суммы совпадают с отчётом источника."""

    AGGREGATE: ClassVar[str] = (
        f'select to_char(city) as "city", month as "month", count(*) as "orders", '
        f'cast(sum(gross) as number(18,2)) as "gross", '
        f'cast(sum(balance) as number(18,2)) as "balance" '
        f"from {OWNER}.orders_report group by to_char(city), month"
    )

    async def test_city_month_comes_back(
        self, source: ClickHouseSide, target: OracleSide
    ) -> None:
        pumps = Pumps(clickhouse=source.profile, oracle=target.profile)
        chained = await pumps.chain(
            Leg(
                "ora_stream_out",
                {
                    "sql": self.AGGREGATE,
                    "columns": (
                        ColumnDeclaration(name="city", nullable=False),
                        ColumnDeclaration(name="month", nullable=False),
                    ),
                },
            ),
            Leg(
                "ch_stream_in",
                {
                    "database": CH_SRC,
                    "table_name": "city_month",
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "create_table": MERGE_TREE,
                    "order_by": "(city, month)",
                },
            ),
        )
        print(f"\n--- {target.source.name} -> {source.source.name} ---")
        print(chained.in_report)
        city_month = ChLoaded(source, "city_month")
        view = ChLoaded(source, "orders_report")

        assert await city_month.types() == [
            ("city", "String"),
            ("month", "DateTime64(0)"),
            ("orders", "Nullable(Decimal(38, 0))"),
            ("gross", "Nullable(Decimal(18, 2))"),
            ("balance", "Nullable(Decimal(18, 2))"),
        ]
        assert await city_month.scalar("sum(orders)") == ORDERS
        assert await city_month.scalar("sum(gross)") == await view.scalar("sum(gross)")
        assert await city_month.scalar("sum(balance)") == await view.scalar(
            "sum(balance)"
        )


async def test_rows_reached_every_column(source: ClickHouseSide) -> None:
    view = ChLoaded(source, "orders_report")

    assert await view.scalar("countIf(paid is null)") > 0
    assert await view.scalar("countIf(payment_state = 'partial')") > 0
    assert await view.scalar("countIf(position(note, '\\t') > 0)") > 0
    assert await view.count() == ORDERS
