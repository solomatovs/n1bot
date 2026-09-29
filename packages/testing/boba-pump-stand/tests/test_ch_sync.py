# ruff: noqa: S608
"""Приёмник ch_stream_in на потоке arrow: pg_stream_out и ch_stream_out в каждый
ClickHouse стенда.

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

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.target import ChStreamWire, ChTableRef
from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.transfer import PgColumnDeclaration
from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Leg,
    Loaded,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.stand.names import StandNames
from boba.toolkit.contract import ColumnDeclaration
from boba.toolkit.transfer import (
    BackupAndCreateIfSchemaChanged,
    ColumnRules,
    CreateIfNotExists,
    DeleteNothing,
    DeleteTruncate,
    DeleteWhere,
    DoNothing,
    DropAndCreate,
    ErrorIfNotExists,
    ErrorIfSchemaChanged,
    FailOnUnknown,
    FallbackAsVarchar,
    InsertFull,
    InsertNothing,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = StandNames().of("pump_ch_sync")
CH_DATABASE = StandNames().of("pump_ch_sync")
ROWS = 60
CHUNK = 65536
STRING_AS_STRING = "output_format_arrow_string_as_string = 1"
MERGE_TREE = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree order by {order_by}"
)
"""Шаблон для серверов без Keeper: дефолт ch_stream_in — ReplicatedMergeTree."""
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

SELECT = f"select * from {PG_SCHEMA}.src"
DECLARED = (
    PgColumnDeclaration(name="id", nullable=False),
    PgColumnDeclaration(name="vc", nullable=False),
)

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


async def land(  # noqa: PLR0913
    postgres: PostgresSide,
    clickhouse: ClickHouseSide,
    table: str,
    schema: Any,
    delete: Any = DeleteNothing(kind="nothing"),
    insert: Any = InsertFull(kind="full"),
    rules: ColumnRules = ColumnRules(),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
    select: str = SELECT,
    create_table: str = MERGE_TREE,
    database: str = CH_DATABASE,
    cluster: str = "",
    order_by: str = ChTableRef.ORDER_BY,
    columns: Sequence[PgColumnDeclaration] = DECLARED,
) -> str:
    """pg_stream_out потоком arrow -> ch_stream_in."""
    pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.profile)
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
        Leg(
            "ch_stream_in",
            {
                "database": database,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "unknown_types": unknown_types,
                "create_table": create_table,
                "cluster": cluster,
                "order_by": order_by,
            },
        ),
    )
    print(f"\n--- {clickhouse.source.name}: ch_stream_in ---\n{chained.in_report}")

    return chained.in_report


class TestCreate:
    async def test_types_and_values_land(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "orders",
            DropAndCreate(kind="drop_and_create"),
            select=f"{SELECT} where id <> 0",
        )
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
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await land(postgres, clickhouse, "twin", DropAndCreate(kind="drop_and_create"))

        report = await land(
            postgres,
            clickhouse,
            "twin",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
        )

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "twin").count() == ROWS
        assert await ChLoaded(clickhouse, "twin__ex").count() == ROWS

    async def test_delete_where_replaces_only_the_matching_rows(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        part = ROWS // 3
        await clickhouse.command(
            f"alter table {CH_DATABASE}.twin update t = 'stale' where id <= {part}",
            settings={"mutations_sync": 2},
        )

        report = await land(
            postgres,
            clickhouse,
            "twin",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(kind="delete_where", where=f"id <= {part}"),
            select=f"{SELECT} where id <= {part}",
        )
        landed = ChLoaded(clickhouse, "twin")

        assert f"{part} rows matching" in report
        assert f"{part} rows loaded" in report
        assert await landed.count() == ROWS
        assert await landed.scalar("countIf(t = 'stale')") == 0
        assert await landed.column("t") == await Loaded(
            postgres, PG_SCHEMA, "src"
        ).texts("t")

    async def test_delete_nothing_appends(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "twin",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteNothing(kind="nothing"),
            select=f"{SELECT} where id <= 10",
        )

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
    RULES: ClassVar[ColumnRules] = ColumnRules(
        rename_columns={
            "order_id": "id",
            "label": "vc",
            "amount": "n",
            "moment": "tz",
        },
        column_types={"amount": "Nullable(Decimal(20, 4))"},
    )
    SELECT: ClassVar[str] = f"select id, vc, n, tz from {PG_SCHEMA}.src"

    async def test_missing_mart_is_refused(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.mart")

        with pytest.raises(TransferError, match="table is missing"):
            await land(
                postgres,
                clickhouse,
                "mart",
                ErrorIfNotExists(kind="error_if_not_exists"),
                rules=self.RULES,
                select=self.SELECT,
            )

    async def test_mart_is_filled_by_its_own_names(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(self.MART)
        source = Loaded(postgres, PG_SCHEMA, "src")

        report = await land(
            postgres,
            clickhouse,
            "mart",
            ErrorIfNotExists(kind="error_if_not_exists"),
            rules=self.RULES,
            select=self.SELECT,
        )
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
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await land(
            postgres,
            clickhouse,
            "declared",
            DropAndCreate(kind="drop_and_create"),
            rules=ColumnRules(
                column_types={
                    "n": "Decimal(20, 4)",
                    "t": "LowCardinality(String)",
                    "vc": "LowCardinality(String)",
                }
            ),
            select=f"select id, n, t, vc from {PG_SCHEMA}.src",
        )
        types = dict(await ChLoaded(clickhouse, "declared").types())

        assert types["n"] == "Nullable(Decimal(20, 4))"
        assert types["t"] == "LowCardinality(Nullable(String))"
        assert types["vc"] == "LowCardinality(String)"

    async def test_null_into_a_plain_column_is_refused(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.plain")
        await clickhouse.command(
            f"create table {CH_DATABASE}.plain (id Int64, i4 Int32) "
            "engine = MergeTree order by id"
        )
        await clickhouse.command(f"insert into {CH_DATABASE}.plain values (0, 0)")

        with pytest.raises(ClickHouseQueryError, match="NULL"):
            await land(
                postgres,
                clickhouse,
                "plain",
                DoNothing(kind="do_nothing"),
                DeleteTruncate(kind="truncate"),
                select=f"select id, i4 from {PG_SCHEMA}.src",
                columns=DECLARED[:1],
            )

        assert await ChLoaded(clickhouse, "plain").count() == 1


class TestSchemaDrift:
    async def test_drift_is_refused_then_backed_up(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await land(postgres, clickhouse, "drift", DropAndCreate(kind="drop_and_create"))
        await clickhouse.command(
            f"alter table {CH_DATABASE}.drift modify column i4 Nullable(Int16)",
            settings={"mutations_sync": 2},
        )

        with pytest.raises(TransferError, match="i4"):
            await land(
                postgres,
                clickhouse,
                "drift",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                DeleteTruncate(kind="truncate"),
            )

        report = await land(
            postgres,
            clickhouse,
            "drift",
            BackupAndCreateIfSchemaChanged(kind="backup_and_create_if_schema_changed"),
        )
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
    TEMPLATE: ClassVar[str] = (
        "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
        "engine = ReplacingMergeTree order by {order_by} "
        "settings index_granularity = 1024"
    )

    async def test_engine_and_order_come_from_the_template(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "templated",
            DropAndCreate(kind="drop_and_create"),
            create_table=self.TEMPLATE,
            order_by="id",
        )
        table = ChLoaded(clickhouse, "templated")
        engine = await table.engine()

        assert f"{ROWS} rows loaded" in report
        assert engine == "ReplacingMergeTree"
        assert await table.sorting_key() == "id"
        assert await table.count() == ROWS

    async def test_template_with_schema_name_is_refused(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        with pytest.raises(TransferError, match="unknown variable \\{schema_name\\}"):
            await land(
                postgres,
                clickhouse,
                "templated_bad",
                DropAndCreate(kind="drop_and_create"),
                create_table=(
                    "create table {schema_name}.{table_name} ({columns}) "
                    "engine = MergeTree order by tuple()"
                ),
            )


class TestUnknownTypes:
    """Массив postgres в нейтральном контракте — семейство array, пары у
    ClickHouse нет: ошибка с подсказкой, по fallback_as_varchar — String с
    текстом массива postgres."""

    SELECT: ClassVar[str] = (
        f"select id, vc, array[id, id + 1]::int[] as arr from {PG_SCHEMA}.src"
    )

    async def test_array_is_refused_by_default(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        with pytest.raises(TransferError, match="column arr: the target clickhouse"):
            await land(
                postgres,
                clickhouse,
                "arrays",
                DropAndCreate(kind="drop_and_create"),
                select=self.SELECT,
            )

    async def test_array_lands_as_string_on_fallback(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await land(
            postgres,
            clickhouse,
            "arrays",
            DropAndCreate(kind="drop_and_create"),
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
            select=self.SELECT,
        )
        table = ChLoaded(clickhouse, "arrays")

        assert ("arr", "Nullable(String)") in await table.types()
        assert (await table.column("arr"))[:2] == ["{1,2}", "{2,3}"]


class TestDryRun:
    async def test_nothing_changes(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        table = ChLoaded(clickhouse, "orders")
        types = await table.types()

        report = await land(
            postgres,
            clickhouse,
            "orders",
            DoNothing(kind="do_nothing"),
            insert=InsertNothing(kind="nothing"),
        )

        assert report.startswith("0 rows loaded")
        assert await table.types() == types
        assert await table.count() == ROWS


class TestClickHouseCircle:
    """ClickHouse -> ClickHouse: ch_stream_out читает загруженную таблицу потоком
    arrow, ch_stream_in кладёт её в новую. UUID ClickHouse до 26 в Arrow не
    выгружает, поэтому запрос отдаёт его toString, как советует etl_skill."""

    async def test_orders_come_around(self, clickhouse: ClickHouseSide) -> None:
        pumps = Pumps(clickhouse=clickhouse.profile)
        chained = await pumps.chain(
            Leg(
                "ch_stream_out",
                {
                    "sql": (
                        f"select id, i4, n, f8, t, vc, d, ts, tz, toString(u) as u "
                        f"from {CH_DATABASE}.orders settings {STRING_AS_STRING}"
                    ),
                    "wire": ChStreamWire.ARROW,
                    "chunk_bytes": CHUNK,
                },
            ),
            Leg(
                "ch_stream_in",
                {
                    "database": CH_DATABASE,
                    "table_name": "circle",
                    "create_table": MERGE_TREE,
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                },
            ),
        )
        circle = ChLoaded(clickhouse, "circle")
        orders = ChLoaded(clickhouse, "orders")

        assert f"{ROWS} rows loaded" in chained.in_report
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

    async def transfer(
        self, clickhouse: ClickHouseSide, table: str, schema: Any, **extra: Any
    ) -> str:
        pumps = Pumps(clickhouse=clickhouse.profile)
        arguments: dict[str, Any] = {
            "database": CH_DATABASE,
            "table_name": table,
            "create_table": MERGE_TREE,
            "order_by": "id",
            "schema_strategy": schema,
            "delete_strategy": DeleteNothing(kind="nothing"),
            "insert_strategy": InsertFull(kind="full"),
        }
        arguments.update(extra)
        chained = await pumps.chain(
            Leg(
                "ch_stream_out",
                {
                    "sql": f"select * from {CH_DATABASE}.typed order by id",
                    "wire": ChStreamWire.TSV,
                    "chunk_bytes": CHUNK,
                },
            ),
            Leg("ch_stream_in", arguments),
        )
        print(f"\n--- {clickhouse.source.name}: tsv circle ---\n{chained.in_report}")

        return chained.in_report

    async def test_types_travel_as_they_are(self, clickhouse: ClickHouseSide) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed")
        await clickhouse.command(self.TYPED)
        await clickhouse.command(self.FILL)
        typed = ChLoaded(clickhouse, "typed")

        report = await self.transfer(
            clickhouse, "typed_copy", DropAndCreate(kind="drop_and_create")
        )
        copy = ChLoaded(clickhouse, "typed_copy")

        assert f"{ROWS} rows loaded" in report
        assert await copy.types() == await typed.types()
        assert await copy.count() == ROWS
        for expression in self.EXPRESSIONS:
            assert await copy.column(expression) == await typed.column(expression), (
                expression
            )

    async def test_reload_passes_the_check_by_type_texts(
        self, clickhouse: ClickHouseSide
    ) -> None:
        report = await self.transfer(
            clickhouse,
            "typed_copy",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            delete_strategy=DeleteTruncate(kind="truncate"),
        )

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "typed_copy").count() == ROWS

    async def test_type_drift_is_refused(self, clickhouse: ClickHouseSide) -> None:
        await clickhouse.command(
            f"alter table {CH_DATABASE}.typed_copy modify column big Nullable(Float64)",
            settings={"mutations_sync": 2},
        )

        with pytest.raises(TransferError, match="big: type differs"):
            await self.transfer(
                clickhouse,
                "typed_copy",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

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
        self, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed_wider")
        await clickhouse.command(self.WIDER)

        report = await self.transfer(
            clickhouse,
            "typed_wider",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
        )
        wider = ChLoaded(clickhouse, "typed_wider")
        typed = ChLoaded(clickhouse, "typed")

        assert f"{ROWS} rows loaded" in report
        assert '"columns": "id"' in report
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
        self, clickhouse: ClickHouseSide, column: str, table_type: str, message: str
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed_narrow")
        await clickhouse.command(
            self.TYPED.replace("typed", "typed_narrow").replace(
                self.COLUMNS[column], f"{column} {table_type}"
            )
        )

        with pytest.raises(TransferError, match=f"{column}: {message}"):
            await self.transfer(
                clickhouse,
                "typed_narrow",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

    async def test_renamed_and_retyped_columns(
        self, clickhouse: ClickHouseSide
    ) -> None:
        report = await self.transfer(
            clickhouse,
            "typed_mart",
            DropAndCreate(kind="drop_and_create"),
            order_by="key",
            rules=ColumnRules(
                rename_columns={"key": "id", "label": "lc"},
                column_types={"label": "String", "big": "Decimal(76, 10)"},
            ),
        )
        types = dict(await ChLoaded(clickhouse, "typed_mart").types())

        assert f"{ROWS} rows loaded" in report
        assert types["key"] == "Int64"
        assert types["label"] == "String"
        assert types["big"] == "Nullable(Decimal(76, 10))"

    async def test_columns_are_refused_with_tsv(
        self, clickhouse: ClickHouseSide
    ) -> None:
        pumps = Pumps(clickhouse=clickhouse.profile)
        with pytest.raises(TransferError, match="columns apply to wire arrow"):
            await pumps.chain(
                Leg(
                    "ch_stream_out",
                    {
                        "sql": f"select id from {CH_DATABASE}.typed",
                        "wire": ChStreamWire.TSV,
                        "columns": [ColumnDeclaration(name="id", nullable=False)],
                        "chunk_bytes": CHUNK,
                    },
                ),
                Leg(
                    "ch_stream_in",
                    {
                        "database": CH_DATABASE,
                        "table_name": "typed_refused",
                        "create_table": MERGE_TREE,
                        "schema_strategy": DropAndCreate(kind="drop_and_create"),
                        "delete_strategy": DeleteNothing(kind="nothing"),
                        "insert_strategy": InsertFull(kind="full"),
                    },
                ),
            )


class TestOrdinaryDatabase:
    async def test_non_atomic_database_is_refused(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        ordinary = f"{CH_DATABASE}_ordinary"
        try:
            await clickhouse.command(
                f"create database if not exists {ordinary} engine = Ordinary",
                settings={"allow_deprecated_database_ordinary": 1},
            )
        except Exception as exc:
            pytest.skip(f"the server does not create Ordinary databases: {exc}")

        try:
            with pytest.raises(TransferError, match="needs an Atomic database"):
                await land(
                    postgres,
                    clickhouse,
                    "orders",
                    CreateIfNotExists(kind="create_if_not_exists"),
                    database=ordinary,
                )
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
        if STAND_CLUSTER not in clusters:
            pytest.skip(f"{clickhouse.source.name} has no cluster {STAND_CLUSTER}")

    async def test_default_template_needs_a_cluster(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        with pytest.raises(ClickHouseQueryError, match="ON CLUSTER"):
            await land(
                postgres,
                clickhouse,
                "replicated",
                DropAndCreate(kind="drop_and_create"),
                create_table=ChTableRef.CREATE_TABLE,
                order_by="id",
            )

    async def test_unknown_cluster_is_refused_before_ddl(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        with pytest.raises(TransferError, match="cluster 'nope' is not in"):
            await land(
                postgres,
                clickhouse,
                "replicated",
                DropAndCreate(kind="drop_and_create"),
                create_table=ChTableRef.CREATE_TABLE,
                cluster="nope",
                order_by="id",
            )

    async def test_replicated_table_loads_through_the_twin(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        table = ChLoaded(clickhouse, "replicated")
        report = await land(
            postgres,
            clickhouse,
            "replicated",
            DropAndCreate(kind="drop_and_create"),
            create_table=ChTableRef.CREATE_TABLE,
            cluster=STAND_CLUSTER,
            order_by="id",
        )

        assert f"{ROWS} rows loaded" in report
        assert await table.engine() == "ReplicatedMergeTree"
        assert await table.sorting_key() == "id"
        assert await table.count() == ROWS

        part = ROWS // 6
        report = await land(
            postgres,
            clickhouse,
            "replicated",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(kind="delete_where", where=f"id <= {part}"),
            select=f"{SELECT} where id <= {part}",
            create_table=ChTableRef.CREATE_TABLE,
            cluster=STAND_CLUSTER,
            order_by="id",
        )
        twin = ChLoaded(clickhouse, "replicated__ex")

        assert f"{part} rows matching" in report
        assert await table.count() == ROWS
        assert await twin.engine() == "ReplicatedMergeTree"
        assert await twin.count() == ROWS
