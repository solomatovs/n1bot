# ruff: noqa: S608, E501
"""Перелив отчёта по заказам из Oracle в postgres и Greenplum парой
Oracle -> postgres (ora_stream_out и pg_stream_in) запросом, каким его написал бы
LLM: CTE, join и left join с NULL, оконные функции, listagg, json_object,
sys_guid, интервалы и TIMESTAMP WITH TIME ZONE, которые приходится
приводить.

Источник — каждый Oracle стенда (12.2, 18, 21, 23), приёмник — каждый
postgres (9.0–19) и Greenplum (6, 7). Сценарии идут по порядку на каждой
паре и опираются на таблицы предыдущих:
    - первая попытка LLM с TIMESTAMP WITH TIME ZONE и интервалом: отказ
      источника с подсказкой, затем запрос переписан (sys_extract_utc,
      секунды числом) и колонка объявлена timestamptz через column_types;
    - первая загрузка отчёта, повторная с проверкой схемы;
    - инкремент одного месяца через delete_where;
    - витрина приёмника со своими именами через rename_columns и column_types;
    - дрейф схемы: отказ error_if_schema_changed, затем бэкап и пересоздание;
    - шаблон create table: distributed by у Greenplum, with (...) у postgres;
    - staging и подмена таблицы шагами after, откат при ошибке шага;
    - сухой прогон do_nothing + insert nothing;
    - двоичный RAW: hex-текст с префиксом \\x и bytea через column_types;
    - обратный путь: агрегат «город × месяц» с приёмника в Oracle.
Запускать из launch.json «pytest: текущий файл»; отчёты насосов печатаются,
видны с -s.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import psycopg
import pytest

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
from boba.stand.names import StandNames
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
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
OWNER = PumpUser().name
DW = StandNames().of("shop_ora_dw")
ARRAYSIZE = 2000
CHUNK = 65536
CUSTOMERS = 12
PRODUCTS = 10
ORDERS = 24
JSONB_SINCE = 90400
JSON_SINCE = 90200

SCHEMA_DDL = (
    """
    create table customers (
        id number(10) not null,
        external_id raw(16) not null,
        name varchar2(60) not null,
        email varchar2(80) not null,
        tier varchar2(10) not null,
        signed_up timestamp(0) with time zone not null,
        city varchar2(20) not null,
        tags varchar2(40) not null,
        last_ip varchar2(15),
        is_active number(1) not null,
        constraint customers_pk primary key (id))""",
    """
    create table products (
        id number(10) not null,
        sku varchar2(32) not null,
        title varchar2(60) not null,
        category varchar2(20) not null,
        price number(12,2) not null,
        weight_kg binary_float,
        constraint products_pk primary key (id))""",
    """
    create table orders (
        id number(10) not null,
        customer_id number(10) not null,
        status varchar2(10) not null,
        placed_at timestamp(6) not null,
        shipped_at timestamp(6),
        note varchar2(200),
        constraint orders_pk primary key (id))""",
    """
    create table order_items (
        id number(10) not null,
        order_id number(10) not null,
        product_id number(10) not null,
        qty number(5) not null,
        unit_price number(12,2) not null,
        discount number(5,4) not null,
        constraint order_items_pk primary key (id))""",
    """
    create table payments (
        id number(10) not null,
        order_id number(10) not null,
        amount number(14,2) not null,
        method varchar2(10) not null,
        paid_at timestamp(6) not null,
        constraint payments_pk primary key (id))""",
)

FILL_DML = (
    f"""
    insert into customers
    select level,
           sys_guid(),
           'Customer ' || level,
           'user' || level || '@Example.COM',
           case mod(level, 3) when 0 then 'bronze' when 1 then 'silver' else 'gold' end,
           from_tz(timestamp '2023-01-01 00:00:00' + numtodsinterval(level, 'hour'), '+03:00'),
           case mod(level, 3) when 0 then 'Moscow' when 1 then 'Kazan' else 'Perm' end,
           case when mod(level, 2) = 0 then 't' || mod(level, 5) || ',vip' else 't' || mod(level, 5) end,
           case when mod(level, 7) = 0 then null else '10.0.' || mod(level, 256) || '.' || mod(level, 250) end,
           case when mod(level, 11) = 0 then 0 else 1 end
    from dual connect by level <= {CUSTOMERS}""",
    f"""
    insert into products
    select level,
           'SKU-' || lpad(level, 6, '0'),
           'Product ' || level,
           case mod(level, 4) when 0 then 'books' when 1 then 'toys' when 2 then 'food' else 'tools' end,
           (1 + mod(level * 7919, 50000)) / 100,
           case when mod(level, 9) = 0 then null else mod(level * 31, 2000) / 100 end
    from dual connect by level <= {PRODUCTS}""",
    f"""
    insert into orders
    select level,
           1 + mod(level * 7919, {CUSTOMERS}),
           case mod(level, 4) when 0 then 'new' when 1 then 'paid' when 2 then 'shipped' else 'cancelled' end,
           timestamp '2024-01-01 00:00:00' + numtodsinterval(level * 849600, 'second'),
           case when mod(level, 3) = 0 then null
                else timestamp '2024-01-01 00:00:00' + numtodsinterval(level * 849600 + mod(level, 96) * 3600, 'second') end,
           case when mod(level, 5) = 0 then 'call before "delivery", floor ' || mod(level, 20) || chr(9) || '\\ back' else null end
    from dual connect by level <= {ORDERS}""",
    f"""
    insert into order_items
    select rownum, x.order_id, x.pid, 1 + mod(x.order_id + x.k, 5), p.price, mod(x.order_id + x.k, 4) * 0.05
    from (
        select o.id as order_id, k.k, 1 + mod(o.id * 31 + k.k * 17, {PRODUCTS}) as pid
        from orders o
        join (select level as k from dual connect by level <= 4) k on k.k <= 1 + mod(o.id, 4)
    ) x
    join products p on p.id = x.pid""",
    """
    insert into payments
    select rownum, o.id,
           case when mod(o.id, 8) = 2 then 10.00 else s.gross end,
           case mod(o.id, 3) when 0 then 'card' when 1 then 'cash' else 'sbp' end,
           o.placed_at + numtodsinterval(15, 'minute')
    from orders o
    join (select order_id, cast(sum(qty * unit_price * (1 - discount)) as number(14,2)) as gross
          from order_items group by order_id) s on s.order_id = o.id
    where o.status in ('paid', 'shipped')""",
    f"""
    insert into payments
    select {ORDERS} * 2 + id, id, 1.00, 'bonus', placed_at + numtodsinterval(1, 'hour')
    from orders
    where status = 'paid' and mod(id, 6) = 1""",
)

REPORT_SQL = f"""
with items as (
    select oi.order_id,
           count(*) as lines,
           sum(oi.qty) as units,
           cast(sum(oi.qty * oi.unit_price * (1 - oi.discount)) as number(14,2)) as gross,
           listagg(p.category, ',') within group (order by p.category) as categories,
           '[' || listagg(json_object('sku' value p.sku, 'qty' value oi.qty), ',')
                  within group (order by oi.id) || ']' as lines_json
    from {OWNER}.order_items oi
    join {OWNER}.products p on p.id = oi.product_id
    group by oi.order_id
),
pay as (
    select p.order_id,
           cast(sum(p.amount) as number(14,2)) as paid_sum,
           max(p.paid_at) as last_paid_at,
           (select listagg(m.method, ',') within group (order by m.method)
              from (select distinct order_id, method from {OWNER}.payments) m
             where m.order_id = p.order_id) as methods
    from {OWNER}.payments p
    group by p.order_id
)
select o.id                                              as "order_id",
       o.status                                          as "status",
       lower(rawtohex(c.external_id))                    as "customer_uid",
       c.name                                            as "customer_name",
       lower(c.email)                                    as "email",
       c.tier                                            as "tier",
       c.city                                            as "city",
       c.tags                                            as "tags",
       c.last_ip                                         as "last_ip",
       c.is_active                                       as "is_active",
       sys_extract_utc(c.signed_up)                      as "signed_up",
       o.placed_at                                       as "placed_at",
       trunc(o.placed_at, 'MM')                          as "month",
       o.shipped_at                                      as "shipped_at",
       round((cast(o.shipped_at as date) - cast(o.placed_at as date)) * 86400) as "lead_time_s",
       nvl(o.note, ' ')                                  as "note",
       i.lines                                           as "lines",
       i.units                                           as "units",
       i.gross                                           as "gross",
       i.categories                                      as "categories",
       i.lines_json                                      as "lines_json",
       pay.paid_sum                                      as "paid",
       pay.last_paid_at                                  as "paid_at",
       pay.methods                                       as "methods",
       case when pay.paid_sum is null then 'unpaid'
            when pay.paid_sum >= i.gross then 'paid'
            else 'partial' end                           as "payment_state",
       cast(i.gross - nvl(pay.paid_sum, 0) as number(14,2)) as "balance",
       row_number() over (partition by c.id order by o.placed_at) as "customer_order_no",
       cast(sum(i.gross) over (partition by c.id) as number(16,2)) as "customer_total",
       cast(round(avg(i.gross) over (partition by trunc(o.placed_at, 'MM')), 6) as number(18,6)) as "month_avg"
from {OWNER}.orders o
join {OWNER}.customers c on c.id = o.customer_id
join items i on i.order_id = o.id
left join pay on pay.order_id = o.id
"""

DECLARED = (
    ColumnDeclaration(name="order_id", nullable=False),
    ColumnDeclaration(name="placed_at", nullable=False),
    ColumnDeclaration(name="month", nullable=False),
)
"""Ключевые поля LLM объявляет not null: сервер считает выборку nullable."""

RULES = ColumnRules(column_types={"signed_up": "timestamptz(0)"})
"""Момент из sys_extract_utc — настенное время UTC: у приёмника он зонный."""

EXPECTED_COLUMNS = [
    ("order_id", "bigint", True),
    ("status", "character varying(10)", False),
    ("customer_uid", "character varying(32)", False),
    ("customer_name", "character varying(60)", False),
    ("email", "character varying(80)", False),
    ("tier", "character varying(10)", False),
    ("city", "character varying(20)", False),
    ("tags", "character varying(40)", False),
    ("last_ip", "character varying(15)", False),
    ("is_active", "bigint", False),
    ("signed_up", "timestamp(0) with time zone", False),
    ("placed_at", "timestamp(6) without time zone", True),
    ("month", "timestamp(0) without time zone", True),
    ("shipped_at", "timestamp(6) without time zone", False),
    ("lead_time_s", "numeric(38,0)", False),
    ("note", "character varying(200)", False),
    ("lines", "numeric(38,0)", False),
    ("units", "numeric(38,0)", False),
    ("gross", "numeric(14,2)", False),
    ("categories", "character varying(4000)", False),
    ("lines_json", "character varying(4000)", False),
    ("paid", "numeric(14,2)", False),
    ("paid_at", "timestamp(6) without time zone", False),
    ("methods", "character varying(4000)", False),
    ("payment_state", "character varying(7)", False),
    ("balance", "numeric(14,2)", False),
    ("customer_order_no", "numeric(38,0)", False),
    ("customer_total", "numeric(16,2)", False),
    ("month_avg", "numeric(18,6)", False),
]
"""Колонки таблицы приёмника: длины строк — из описания стейтмента сервером
Oracle; NUMBER без точности (count, row_number, round) — numeric(38,0).
Признак not null сравнивается отдельно: Oracle сообщает его по колонкам
таблиц и не сообщает по выражениям."""

NOT_NULL = {"order_id", "status", "tier", "city", "placed_at", "month"}
"""Колонки, у которых not null обязан дойти: из таблиц и из деклараций."""


def typed(columns: Sequence[tuple[str, str, bool]]) -> list[tuple[str, str]]:
    return [(name, kind) for name, kind, _ in columns]


def not_null(columns: Sequence[tuple[str, str, bool]]) -> set[str]:
    names: set[str] = set()
    for name, _, required in columns:
        if required:
            names.add(name)

    return names


COMPARED: Mapping[str, tuple[str, str]] = {
    "order_id": ("{c}", "{c}"),
    "is_active": ("{c}", "{c}"),
    "signed_up": (
        "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS')",
        "to_char({c}, 'yyyy-mm-dd hh24:mi:ss')",
    ),
    "placed_at": (
        "to_char({c}, 'YYYY-MM-DD HH24:MI:SS.US')",
        "to_char({c}, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
    "month": ("to_char({c}, 'YYYY-MM-DD')", "to_char({c}, 'yyyy-mm-dd')"),
    "shipped_at": (
        "to_char({c}, 'YYYY-MM-DD HH24:MI:SS.US')",
        "to_char({c}, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
    "paid_at": (
        "to_char({c}, 'YYYY-MM-DD HH24:MI:SS.US')",
        "to_char({c}, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
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
"""Как сравнить колонку отчёта: выражение postgres и выражение Oracle с
одинаковым значением. Числа — значением, время — текстом; остальные —
::text против самой колонки (строки Oracle приходят строками)."""


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


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def target(request: Any, source: OracleSide) -> AsyncIterator[PostgresSide]:
    """Схема приёмника пересоздаётся на каждый Oracle: сценарии опираются на
    таблицы предыдущих."""
    side = PostgresSide(request.param, DW)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


async def transfer(  # noqa: PLR0913
    source: OracleSide,
    target: PostgresSide,
    table: str,
    schema: Any,
    delete: Any,
    select: str = REPORT_SQL,
    columns: Sequence[ColumnDeclaration] = DECLARED,
    rules: ColumnRules = RULES,
    insert: Any = InsertFull(kind="full"),
    create_table: str = PgTransferTable.CREATE_TABLE,
    after: Sequence[str] = (),
) -> str:
    """ora_stream_out на source -> pg_stream_in на target."""
    pumps = Pumps(oracle=source.profile, postgres=target.profile)
    chained = await pumps.chain(
        Leg("ora_stream_out", {"sql": select, "columns": columns}),
        Leg(
            "pg_stream_in",
            {
                "schema_name": DW,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "create_table": create_table,
                "after": after,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
    )
    print(
        f"\n--- {source.source.name} -> {target.source.name}: ora_stream_out ---\n"
        f"{chained.out_report}"
    )
    print(f"--- pg_stream_in ---\n{chained.in_report}")

    return chained.in_report


def month_of(month: str) -> str:
    """Выборка отчёта за один месяц: окна посчитаны по всему отчёту."""
    return (
        f'select * from ({REPORT_SQL}) r where r."month" = '
        f"to_date('{month}', 'yyyy-mm-dd')"
    )


async def same_content(source: OracleSide, target: PostgresSide, table: str) -> None:
    """Каждая колонка отчёта на приёмнике совпадает с отчётом, снятым в таблицу
    Oracle тем же запросом."""
    report = OraLoaded(source, "orders_report")
    landed = Loaded(target, DW, table)
    for name, _, _ in EXPECTED_COLUMNS:
        entry = COMPARED.get(name)
        if entry is None:
            actual = await landed.texts(name, order_by="order_id")
            expected = await report.column(f'"{name}"', order_by='"order_id"')

            assert actual == expected, f"column {name} differs"

            continue

        pg_template, ora_template = entry
        actual = await landed.scalars(pg_template.format(c=name), order_by="order_id")
        expected = await report.column(
            ora_template.format(c=f'"{name}"'), order_by='"order_id"'
        )
        if ora_template == "{c}":
            assert numbers(actual) == numbers(expected), f"column {name} differs"

            continue

        assert actual == expected, f"column {name} differs"


def numbers(values: Sequence[Any]) -> list[Decimal | None]:
    """Числа обеих сторон значением: Oracle печатает 12.5, postgres 12.50."""
    parsed: list[Decimal | None] = []
    for value in values:
        if value is None:
            parsed.append(None)
            continue

        try:
            parsed.append(Decimal(str(value)))
        except ArithmeticError as exc:
            raise AssertionError(f"not a number: {value!r}") from exc

    return parsed


class TestFirstAttempt:
    """LLM выгружает TIMESTAMP WITH TIME ZONE и интервал как есть: источник
    отказывает до выполнения и подсказывает, чем их привести."""

    async def test_zoned_timestamp_is_refused(
        self, source: OracleSide, target: PostgresSide
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

    async def test_interval_is_refused(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        with pytest.raises(OracleQueryError, match=r"lead_time.*INTERVAL_DS"):
            await transfer(
                source,
                target,
                "orders_raw",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                select=(
                    f'select id as "order_id", shipped_at - placed_at as "lead_time" '
                    f"from {OWNER}.orders"
                ),
                columns=(),
                rules=ColumnRules(),
            )


class TestOrdersReport:
    async def test_report_lands_with_its_types(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
        )
        landed = Loaded(target, DW, "orders_report")

        assert f"{ORDERS} rows loaded" in report
        assert typed(await landed.columns()) == typed(EXPECTED_COLUMNS)
        assert not_null(await landed.columns()) >= NOT_NULL
        assert await landed.count() == ORDERS
        assert await landed.aggregate("count(case when paid is null then 1 end)") > 0
        assert (
            await landed.aggregate("count(case when note like '%\t%' then 1 end)") > 0
        )
        await same_content(source, target, "orders_report")

    async def test_reload_into_the_existing_table_passes_the_check(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
        )

        assert "error" not in report.split("rows loaded")[0].lower()
        assert await Loaded(target, DW, "orders_report").count() == ORDERS


class TestIncrementalMonth:
    """Перезаливка одного месяца в готовую витрину: delete_where по month
    удаляет только его, insert full кладёт свежие строки месяца."""

    MONTH: ClassVar[str] = "2024-03-01"

    async def test_one_month_is_replaced(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")
        await target.execute(
            [
                f"update {DW}.orders_report set note = 'stale' "
                f"where month = timestamp '{self.MONTH}'"
            ]
        )
        in_month = await OraLoaded(source, "orders_report").scalar(
            f"""count(case when "month" = to_date('{self.MONTH}', 'yyyy-mm-dd') then 1 end)"""
        )

        report = await transfer(
            source,
            target,
            "orders_report",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(kind="delete_where", where=f"month = timestamp '{self.MONTH}'"),
            select=month_of(self.MONTH),
        )

        assert in_month > 0
        assert f"{in_month} rows deleted" in report
        assert f"{in_month} rows loaded" in report
        assert await landed.aggregate("count(case when note = 'stale' then 1 end)") == 0
        assert await landed.count() == ORDERS
        await same_content(source, target, "orders_report")


class TestRenamedMart:
    """Витрина приёмника со своими именами и типами: таблица создана заранее,
    error_if_not_exists не даёт её создать заново, rename_columns связывает
    колонки витрины с полями потока, column_types даёт колонке тип витрины."""

    MART: ClassVar[str] = f"""
        create table {DW}.orders_mart (
            order_uid bigint not null,
            client text,
            client_tier varchar(10),
            paid_amount numeric(14,2),
            gross numeric(14,2),
            placed_at timestamp
        )"""
    SELECT: ClassVar[str] = (
        f'select "order_id", "customer_name", "tier", "paid", "gross", "placed_at" '
        f"from ({REPORT_SQL}) r"
    )
    RULES: ClassVar[ColumnRules] = ColumnRules(
        rename_columns={
            "order_uid": "order_id",
            "client": "customer_name",
            "client_tier": "tier",
            "paid_amount": "paid",
        },
        column_types={"client": "text", "client_tier": "varchar(10)"},
    )

    async def test_missing_mart_is_refused(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        with pytest.raises(TransferError, match="table is missing"):
            await transfer(
                source,
                target,
                "orders_mart",
                ErrorIfNotExists(kind="error_if_not_exists"),
                DeleteNothing(kind="nothing"),
                select=self.SELECT,
                columns=(ColumnDeclaration(name="order_id", nullable=False),),
                rules=self.RULES,
            )

    async def test_mart_is_filled_by_its_own_names(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        await target.execute([f"drop table if exists {DW}.orders_mart", self.MART])
        view = OraLoaded(source, "orders_report")
        mart = Loaded(target, DW, "orders_mart")

        report = await transfer(
            source,
            target,
            "orders_mart",
            ErrorIfNotExists(kind="error_if_not_exists"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            columns=(ColumnDeclaration(name="order_id", nullable=False),),
            rules=self.RULES,
        )

        assert f"{ORDERS} rows loaded" in report
        assert await mart.count() == ORDERS
        assert await mart.texts("client", order_by="order_uid") == (
            await view.column('"customer_name"', order_by='"order_id"')
        )
        assert await mart.texts("client_tier", order_by="order_uid") == (
            await view.column('"tier"', order_by='"order_id"')
        )
        assert await mart.aggregate("sum(paid_amount)") == Decimal(
            str(await view.scalar('sum("paid")'))
        )


class TestSchemaDrift:
    """Таблица на приёмнике разошлась с потоком: error_if_schema_changed
    отказывает с текстом расхождения, backup_and_create_if_schema_changed
    переименовывает её в _bak_<время> и создаёт заново."""

    async def test_drift_is_refused_then_backed_up(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        drift = Loaded(target, DW, "orders_drift")
        await transfer(
            source,
            target,
            "orders_drift",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
        )
        await target.execute(
            [f"alter table {DW}.orders_drift alter column gross type numeric(12,1)"]
        )

        with pytest.raises(
            TransferError, match=r"gross: table decimal\(12, 1\) truncates"
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

        assert "saved as" in report
        assert "orders_drift_bak_" in report
        assert len(backups) == 1
        assert await Loaded(target, DW, backups[0]).count() == ORDERS
        assert await drift.count() == ORDERS
        assert ("gross", "numeric(14,2)", False) in await drift.columns()


class TestCreateTemplate:
    """Особенности таблицы из шаблона create table: у Greenplum ключ
    распределения, у postgres параметры хранения."""

    GREENPLUM: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) distributed by (order_id)"
    )
    POSTGRES: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) with (fillfactor = 90)"
    )

    async def test_table_gets_its_storage_options(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        template = self.POSTGRES
        if target.greenplum:
            template = self.GREENPLUM

        for _ in range(2):
            report = await transfer(
                source,
                target,
                "orders_tpl",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                create_table=template,
            )

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

    SWAP: ClassVar[tuple[str, ...]] = (
        f"alter table {DW}.orders_report rename to orders_report_old",
        f"alter table {DW}.orders_report_stage rename to orders_report",
        f"drop table {DW}.orders_report_old",
    )

    async def test_failed_swap_leaves_the_mart_intact(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")

        with pytest.raises(psycopg.Error, match="swap refused"):
            await transfer(
                source,
                target,
                "orders_report_stage",
                DropAndCreate(kind="drop_and_create"),
                DeleteNothing(kind="nothing"),
                after=(
                    *self.SWAP[:2],
                    "do $$ begin raise exception 'swap refused'; end $$",
                ),
            )

        tables = await landed.tables()

        assert "orders_report" in tables
        assert "orders_report_stage" not in tables
        assert await landed.count() == ORDERS

    async def test_swap_replaces_the_mart(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")
        await target.execute([f"update {DW}.orders_report set note = 'old'"])

        report = await transfer(
            source,
            target,
            "orders_report_stage",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            after=self.SWAP,
        )
        tables = await landed.tables()

        assert f"{ORDERS} rows loaded" in report
        assert "orders_report_stage" not in tables
        assert await landed.aggregate("count(case when note = 'old' then 1 end)") == 0
        await same_content(source, target, "orders_report")


class TestDryRun:
    """do_nothing и insert nothing: поток прочитан до конца, таблица не
    тронута ни схемой, ни данными."""

    async def test_nothing_changes(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        landed = Loaded(target, DW, "orders_report")
        columns = await landed.columns()

        report = await transfer(
            source,
            target,
            "orders_report",
            DoNothing(kind="do_nothing"),
            DeleteNothing(kind="nothing"),
            insert=InsertNothing(kind="nothing"),
        )

        assert report.startswith("0 rows loaded")
        assert await landed.columns() == columns
        assert await landed.count() == ORDERS


class TestBinary:
    """RAW едет в postgres hex-текстом: как текст в varchar, как bytea через
    column_types; NULL остаётся NULL, потому что '\\x' || null в Oracle — NULL."""

    SELECT: ClassVar[str] = (
        f'select id as "id", \'\\x\' || rawtohex(external_id) as "uid", '
        f"case when mod(id, 7) = 0 then null else '\\x' || rawtohex(external_id) end "
        f'as "maybe" from {OWNER}.customers'
    )

    async def test_hex_text_lands_as_bytea(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        report = await transfer(
            source,
            target,
            "customer_uids",
            DropAndCreate(kind="drop_and_create"),
            DeleteNothing(kind="nothing"),
            select=self.SELECT,
            columns=(ColumnDeclaration(name="id", nullable=False),),
            rules=ColumnRules(column_types={"maybe": "bytea"}),
        )
        landed = Loaded(target, DW, "customer_uids")

        assert f"{CUSTOMERS} rows loaded" in report
        assert await landed.columns() == [
            ("id", "bigint", True),
            ("uid", "character varying(34)", False),
            ("maybe", "bytea", False),
        ]
        assert await landed.aggregate("count(case when maybe is null then 1 end)") == (
            await OraLoaded(source, "customers").scalar(
                "count(case when mod(id, 7) = 0 then 1 end)"
            )
        )
        assert await landed.scalars("encode(maybe, 'hex')", order_by="id") == (
            await landed.scalars(
                "case when id % 7 = 0 then null else lower(substr(uid, 3)) end",
                order_by="id",
            )
        )


class TestBackToOracle:
    """Обратный путь: агрегат «город × месяц» с приёмника в Oracle парой
    postgres -> Oracle по arrow; суммы совпадают с отчётом источника."""

    AGGREGATE: ClassVar[str] = (
        f"select city, month, count(*) as orders, "
        f"sum(gross)::numeric(16,2) as gross, "
        f"sum(balance)::numeric(16,2) as balance "
        f"from {DW}.orders_report group by city, month"
    )

    async def test_city_month_comes_back(
        self, source: OracleSide, target: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=source.profile, postgres=target.profile)
        chained = await pumps.chain(
            Leg(
                "pg_stream_out",
                {
                    "sql": self.AGGREGATE,
                    "wire": StreamWire.ARROW,
                    "columns": (
                        PgColumnDeclaration(name="city", nullable=False),
                        PgColumnDeclaration(name="month", nullable=False),
                    ),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK),
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
            ("city", "VARCHAR2(20 CHAR)", False),
            ("month", "DATE", True),
            ("orders", "NUMBER(19,0)", False),
            ("gross", "NUMBER(16,2)", False),
            ("balance", "NUMBER(16,2)", False),
        ]
        assert await city_month.scalar("sum(orders)") == ORDERS
        assert await city_month.scalar("sum(gross)") == await view.scalar(
            'sum("gross")'
        )
        assert await city_month.scalar("sum(balance)") == await view.scalar(
            'sum("balance")'
        )


async def test_rows_reached_every_column(source: OracleSide) -> None:
    """Сторож данных стенда: в отчёте есть и NULL левого join, и частичные
    оплаты, и заметки с табуляцией — иначе сценарии выше ничего не ловят."""
    view = OraLoaded(source, "orders_report")

    assert await view.scalar('count(case when "paid" is null then 1 end)') > 0
    assert (
        await view.scalar("""count(case when "payment_state" = 'partial' then 1 end)""")
        > 0
    )
    assert (
        await view.scalar("""count(case when instr("note", chr(9)) > 0 then 1 end)""")
        > 0
    )
    assert await view.scalar('count(case when "last_ip" is null then 1 end)') > 0
    assert await view.count() == ORDERS
