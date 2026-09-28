# ruff: noqa: S608
"""Перелив отчёта по заказам из postgres и Greenplum в Oracle парой
postgres -> Oracle (pg_stream_out с wire = arrow и ora_stream_in) запросом,
каким его написал бы LLM: CTE, join и left join с NULL, оконные функции,
агрегаты в строку, enum, массив, uuid, inet, interval, date_trunc.

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
from typing import Any, ClassVar

import pytest
from test_pg_ch_realistic import (
    DECLARED,
    FILL_DML,
    ORDERS,
    RAW_SQL,
    REPORT_SQL,
    SCHEMA_DDL,
    SRC,
)

from boba.db.oracle import OracleQueryError
from boba.db.oracle.target import OraTableRef
from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.transfer import PgColumnDeclaration, PgTransferTable
from boba.pump_stand import (
    Leg,
    Loaded,
    OracleSide,
    OraLoaded,
    PostgresSide,
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
    FallbackAsVarchar,
    InsertFull,
    InsertNothing,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
OWNER = PumpUser().name
ARRAYSIZE = 2000
CHUNK = 65536
BOOLEAN_SINCE = 23
PG_JSON_SINCE = 90400

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


def compared(target: OracleSide) -> dict[str, tuple[str, str]]:
    """Опорные выражения под версию приёмника: до 23 is_active — число."""
    templates = dict(COMPARED)
    if target.version < BOOLEAN_SINCE:
        templates["is_active"] = ("{c}::int", "{c}")

    return templates


async def transfer(  # noqa: PLR0913
    source: PostgresSide,
    target: OracleSide,
    table: str,
    schema: Any,
    delete: Any,
    select: str = "",
    columns: Sequence[PgColumnDeclaration] = DECLARED,
    rules: ColumnRules = ColumnRules(),
    unknown_types: Any = None,
    insert: Any = InsertFull(kind="full"),
    create_table: str = OraTableRef.CREATE_TABLE,
    after: Sequence[str] = (),
) -> str:
    """pg_stream_out (arrow) на source -> ora_stream_in на target; без select —
    отчёт под версию приёмника."""
    if not select:
        select = report_for(target)

    arguments: dict[str, Any] = {
        "schema_name": OWNER,
        "table_name": table,
        "schema_strategy": schema,
        "delete_strategy": delete,
        "insert_strategy": insert,
        "rules": rules,
        "create_table": create_table,
        "chunk_bytes": CHUNK,
        "after": after,
    }
    if unknown_types is not None:
        arguments["unknown_types"] = unknown_types

    pumps = Pumps(postgres=source.profile, oracle=target.profile)
    chained = await pumps.chain(
        Leg(
            "pg_stream_out",
            {
                "sql": select,
                "wire": StreamWire.ARROW,
                "columns": columns,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
        Leg("ora_stream_in", arguments),
    )
    print(
        f"\n--- {source.source.name} -> {target.source.name}: "
        f"pg_stream_out (arrow) ---\n"
        f"{chained.out_report}"
    )
    print(f"--- ora_stream_in ---\n{chained.in_report}")

    return chained.in_report


def month_of(target: OracleSide, month: str) -> str:
    return report_for(target, f"where r.month = date '{month}'")


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

    RAW_COLUMNS: ClassVar[tuple[PgColumnDeclaration, ...]] = (
        PgColumnDeclaration(name="order_id", nullable=False),
    )

    async def test_enum_is_refused(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        with pytest.raises(TransferError, match="column status: "):
            await transfer(
                source,
                target,
                "raw_orders",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                select=RAW_SQL,
                columns=self.RAW_COLUMNS,
            )

    async def test_fallback_lands_clobs(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "raw_orders",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=RAW_SQL,
            columns=self.RAW_COLUMNS,
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        landed = OraLoaded(target, "raw_orders")
        view = Loaded(source, SRC, "raw_orders")

        assert f"{ORDERS} rows written" in report
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
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
        )
        landed = OraLoaded(target, "orders_report")

        assert f"{ORDERS} rows written" in report
        assert await landed.columns() == expected_columns(target)
        assert await landed.count() == ORDERS
        assert await landed.scalar("count(case when paid is null then 1 end)") > 0
        assert (
            await landed.scalar("count(case when instr(note, chr(9)) > 0 then 1 end)")
            > 0
        )
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
        )

        assert "error" not in report.split("rows written")[0].lower()
        assert await OraLoaded(target, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    MONTH: ClassVar[str] = "2024-03-01"

    async def test_one_month_is_replaced(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        await target.run(
            (
                f"update {OWNER}.orders_report set note = 'stale' "
                f"where month = to_date('{self.MONTH}', 'yyyy-mm-dd')",
            )
        )
        in_month = await Loaded(source, SRC, "orders_report").aggregate(
            f"count(case when month = date '{self.MONTH}' then 1 end)"
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
            select=month_of(target, self.MONTH),
        )

        assert in_month > 0
        assert f"deleted: {in_month} rows" in report
        assert f"{in_month} rows written" in report
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
    SELECT: ClassVar[str] = (
        f"select order_id, customer_name, tier, paid, gross, placed_at "
        f"from ({REPORT_SQL}) r"
    )
    COLUMNS: ClassVar[tuple[PgColumnDeclaration, ...]] = (
        PgColumnDeclaration(name="order_id", nullable=False),
    )
    RULES: ClassVar[ColumnRules] = ColumnRules(
        rename_columns={
            "order_uid": "order_id",
            "client": "customer_name",
            "client_tier": "tier",
            "paid_amount": "paid",
        },
        column_types={"client": "VARCHAR2(60 CHAR)", "client_tier": "VARCHAR2(10)"},
    )

    async def test_missing_mart_is_refused(
        self, source: PostgresSide, target: OracleSide
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
            )

    async def test_mart_is_filled_by_its_own_names(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        await target.run((self.MART,))
        view = Loaded(source, SRC, "orders_report")
        mart = OraLoaded(target, "orders_mart")

        report = await transfer(
            source,
            target,
            "orders_mart",
            ErrorIfNotExists(kind="error_if_not_exists"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            columns=self.COLUMNS,
            rules=self.RULES,
        )

        assert f"{ORDERS} rows written" in report
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
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        drift = OraLoaded(target, "orders_drift")
        await transfer(
            source,
            target,
            "orders_drift",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
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
        assert await OraLoaded(target, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("gross", "NUMBER(14,2)", False) in await drift.columns()


class TestCreateTemplate:
    """Шаблон create table: tablespace и nologging; drop_and_create дважды
    подряд пересоздаёт таблицу по тому же шаблону."""

    TEMPLATE: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) tablespace users nologging"
    )

    async def test_table_gets_its_storage_options(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        for _ in range(2):
            report = await transfer(
                source,
                target,
                "orders_tpl",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                create_table=self.TEMPLATE,
            )

            assert f"{ORDERS} rows written" in report

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

    SWAP: ClassVar[tuple[str, ...]] = (
        f"alter table {OWNER}.orders_report rename to orders_report_old",
        f"alter table {OWNER}.orders_report_stage rename to orders_report",
        f"drop table {OWNER}.orders_report_old purge",
    )

    async def test_failed_step_rolls_back_the_rows(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")

        with pytest.raises(OracleQueryError, match="ORA-00942"):
            await transfer(
                source,
                target,
                "orders_report_stage",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                after=("insert into no_such_table values (1)",),
            )

        tables = await landed.tables()

        assert "orders_report" in tables
        assert "orders_report_stage" in tables
        assert await OraLoaded(target, "orders_report_stage").count() == 0
        assert await landed.count() == ORDERS

    async def test_swap_replaces_the_mart(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        await target.run((f"update {OWNER}.orders_report set note = 'old'",))

        report = await transfer(
            source,
            target,
            "orders_report_stage",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            after=self.SWAP,
        )
        tables = await landed.tables()

        assert f"{ORDERS} rows written" in report
        assert "orders_report_stage" not in tables
        assert "orders_report_old" not in tables
        assert (
            await landed.scalar("count(case when to_char(note) = 'old' then 1 end)")
            == 0
        )
        await same_content(source, target, "orders_report")


class TestDryRun:
    async def test_nothing_changes(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        landed = OraLoaded(target, "orders_report")
        columns = await landed.columns()

        report = await transfer(
            source,
            target,
            "orders_report",
            DoNothing(kind="do_nothing"),
            DeleteNothing(kind="nothing"),
            insert=InsertNothing(kind="nothing"),
        )

        assert report.startswith("0 rows written")
        assert await landed.columns() == columns
        assert await landed.count() == ORDERS


class TestJson:
    """Профиль клиента jsonb источника ложится CLOB и читается json-функциями
    Oracle. Источники до 9.4 json_build_object не умеют."""

    SELECT: ClassVar[str] = f"""
        select c.id as customer_id,
               json_build_object('city', c.city, 'tier', c.tier::text,
                                 'tags', array_to_json(c.tags))::jsonb as profile
        from {SRC}.customers c"""
    COLUMNS: ClassVar[tuple[PgColumnDeclaration, ...]] = (
        PgColumnDeclaration(name="customer_id", nullable=False),
        PgColumnDeclaration(name="profile", nullable=False),
    )

    async def test_profiles_land_as_clob(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        if source.version < PG_JSON_SINCE:
            pytest.skip("json_build_object and jsonb appear in 9.4")

        report = await transfer(
            source,
            target,
            "profiles",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            columns=self.COLUMNS,
        )
        landed = OraLoaded(target, "profiles")
        source_cities = await Loaded(source, SRC, "customers").texts("city")

        assert "rows written" in report
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

    AGGREGATE: ClassVar[str] = (
        f'select to_char(city) as "city", month as "month", count(*) as "orders", '
        f'cast(sum(gross) as number(16,2)) as "gross", '
        f'cast(sum(balance) as number(16,2)) as "balance" '
        f"from {OWNER}.orders_report group by to_char(city), month"
    )

    async def test_city_month_comes_back(
        self, source: PostgresSide, target: OracleSide
    ) -> None:
        pumps = Pumps(postgres=source.profile, oracle=target.profile)
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
                "pg_stream_in",
                {
                    "schema_name": SRC,
                    "table_name": "city_month",
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "create_table": PgTransferTable.CREATE_TABLE,
                    "copy_options": CopyOptions(chunk_bytes=CHUNK),
                },
            ),
        )
        print(f"\n--- {target.source.name} -> {source.source.name} ---")
        print(chained.in_report)
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
