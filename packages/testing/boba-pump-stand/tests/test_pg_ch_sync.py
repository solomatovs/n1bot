# ruff: noqa: S608
"""Пара postgres -> ClickHouse по tsv: pg_stream_out с wire = tsv в ch_stream_in на
каждом ClickHouse стенда, источник — новейший postgres с таблицей всех
ходовых типов.

Что проверяется:
    - каждый тип postgres ложится своим типом ClickHouse, значения совпадают
      текстом; json и jsonb — JSON, где сервер его умеет, nullable jsonb —
      Nullable(JSON) только с 25.x, иначе String;
    - типы без пары (массивы, numeric без точности или шире 76 разрядов,
      enum) — отказ с подсказкой или String по fallback_as_varchar;
    - ловушки сервера: inet с маской и массив на верхнем уровне json — ошибка
      при загрузке, таблица не меняется; даты вне 1900–2299 прижимаются;
    - повторная загрузка со сверкой, витрина через rename и column_types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.target import ChTableRef
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
from boba.toolkit.transfer import (
    ColumnRules,
    DeleteNothing,
    DeleteTruncate,
    DropAndCreate,
    ErrorIfNotExists,
    ErrorIfSchemaChanged,
    FailOnUnknown,
    FallbackAsVarchar,
    InsertFull,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = StandNames().of("pump_pg_ch")
CH_DATABASE = StandNames().of("pump_pg_ch")
ROWS = 60
CHUNK = 65536
MERGE_TREE = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree order by {order_by}"
)
NULLABLE_JSON_SINCE = 25

SOURCE_DDL = f"""
create table {PG_SCHEMA}.src (
    id bigint not null,
    i2 smallint,
    i4 integer,
    n numeric(18,4),
    nfree numeric,
    nwide numeric(80,2),
    f4 real,
    f8 double precision,
    b boolean,
    t text,
    vc varchar(20) not null,
    c5 char(5),
    by bytea,
    d date,
    ts timestamp(6),
    tz timestamptz(3),
    tm time,
    iv interval,
    u uuid,
    j json,
    jb jsonb not null,
    ip inet,
    net cidr,
    mac macaddr,
    mo money,
    bt bit(4),
    xm xml,
    ia integer[],
    en mood,
    tr int4range
)"""

SOURCE_FILL = f"""
insert into {PG_SCHEMA}.src
select g,
       case when g % 7 = 0 then null else (g % 30000)::smallint end,
       case when g % 7 = 0 then null else g * 3 end,
       case when g % 11 = 0 then null else (g * 1.2345)::numeric(18,4) end,
       (g * 1.5)::numeric,
       (g * 1e60)::numeric(80,2),
       case when g % 13 = 0 then null else (g / 7.0)::real end,
       case when g % 13 = 0 then null else g / 7.0 end,
       case when g % 5 = 0 then null else g % 2 = 0 end,
       case when g % 9 = 0 then null else 'text ' || g || E' \\t tab \\\\ back' end,
       'label ' || g % 50,
       'ab',
       decode(lpad(to_hex(g), 8, '0'), 'hex'),
       date '2024-01-01' + g % 400,
       timestamp '2024-01-01 00:00:00.123456' + (g || ' minutes')::interval,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' seconds')::interval,
       time '13:14:15' + (g || ' seconds')::interval,
       (g % 90 || ' minutes')::interval,
       md5(g::text)::uuid,
       case when g % 3 = 0 then null
            else json_build_object('g', g, 'tag', 'x' || g % 3) end,
       jsonb_build_object('g', g, 'nested', jsonb_build_object('k', g % 3)),
       ('10.1.' || g % 256 || '.' || g % 250)::inet,
       '10.0.0.0/8'::cidr,
       '08:00:2b:01:02:03'::macaddr,
       (g % 1000)::numeric::money,
       B'1010',
       ('<a>' || g || '</a>')::xml,
       array[g, g + 1],
       (array['sad', 'happy'])[1 + g % 2]::mood,
       int4range(g, g + 10)
from generate_series(1, {ROWS}) g"""

NATIVE = (
    "id, i2, i4, n, f4, f8, b, t, vc, c5, by, d, ts, tz, tm, iv, u, j, jb, ip, "
    "net, mac, mo, bt, xm, tr"
)
SELECT = f"select {NATIVE} from {PG_SCHEMA}.src"
DECLARED = (
    PgColumnDeclaration(name="id", nullable=False),
    PgColumnDeclaration(name="vc", nullable=False),
    PgColumnDeclaration(name="jb", nullable=False),
)

EXPECTED_TYPES = {
    "id": "Int64",
    "i2": "Nullable(Int16)",
    "i4": "Nullable(Int32)",
    "n": "Nullable(Decimal(18, 4))",
    "f4": "Nullable(Float32)",
    "f8": "Nullable(Float64)",
    "b": "Nullable(Bool)",
    "t": "Nullable(String)",
    "vc": "String",
    "c5": "Nullable(String)",
    "by": "Nullable(String)",
    "d": "Nullable(Date32)",
    "ts": "Nullable(DateTime64(6))",
    "tz": "Nullable(DateTime64(3, 'UTC'))",
    "tm": "Nullable(String)",
    "iv": "Nullable(String)",
    "u": "Nullable(UUID)",
    "net": "Nullable(String)",
    "mac": "Nullable(String)",
    "mo": "Nullable(String)",
    "bt": "Nullable(String)",
    "xm": "Nullable(String)",
    "tr": "Nullable(String)",
}
"""Типы ClickHouse по контракту postgres, кроме json и jsonb: они зависят от
версии сервера."""


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
    await side.recreate_schema(
        ["create type mood as enum ('sad', 'happy')", SOURCE_DDL, SOURCE_FILL]
    )

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
    rules: ColumnRules = ColumnRules(),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
    select: str = SELECT,
    columns: Sequence[PgColumnDeclaration] = DECLARED,
    order_by: str = ChTableRef.ORDER_BY,
) -> str:
    """pg_stream_out с wire = tsv -> ch_stream_in."""
    pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.profile)
    chained = await pumps.chain(
        Leg(
            "pg_stream_out",
            {
                "sql": select,
                "wire": StreamWire.TSV,
                "columns": columns,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
        Leg(
            "ch_stream_in",
            {
                "database": CH_DATABASE,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": InsertFull(kind="full"),
                "rules": rules,
                "unknown_types": unknown_types,
                "create_table": MERGE_TREE,
                "order_by": order_by,
            },
        ),
    )
    print(f"\n--- {clickhouse.source.name}: pg -> ch tsv ---\n{chained.in_report}")

    return chained.in_report


IPV4_IN_IPV6_SINCE = 23


def inet_type(clickhouse: ClickHouseSide) -> dict[str, str]:
    """inet postgres: IPv6, где TabSeparated читает текст IPv4 в IPv6, иначе
    String."""
    if clickhouse.major < IPV4_IN_IPV6_SINCE:
        return {"ip": "Nullable(String)"}

    return {"ip": "Nullable(IPv6)"}


def json_types(clickhouse: ClickHouseSide) -> dict[str, str]:
    """Типы json-колонок по версии сервера: до 24 — Object('json'), nullable
    JSON только с 25."""
    if clickhouse.major < 24:
        return {"jb": "Object('json')", "j": "Nullable(String)"}

    if clickhouse.major < NULLABLE_JSON_SINCE:
        return {"jb": "JSON", "j": "Nullable(String)"}

    return {"jb": "JSON", "j": "Nullable(JSON)"}


class TestNativeTypes:
    async def test_types_and_values_land(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres, clickhouse, "types", DropAndCreate(kind="drop_and_create")
        )
        landed = ChLoaded(clickhouse, "types")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            **EXPECTED_TYPES,
            **json_types(clickhouse),
            **inet_type(clickhouse),
        }
        assert await landed.count() == ROWS
        assert Decimal(str(await landed.scalar("sum(n)"))) == await source.aggregate(
            "sum(n)"
        )
        assert await landed.scalar("sum(i4)") == await source.aggregate("sum(i4)")
        assert await landed.scalar("countIf(b)") == await source.aggregate(
            "count(*) filter (where b)"
        )
        assert await landed.scalar("countIf(b is null)") == await source.aggregate(
            "count(*) filter (where b is null)"
        )
        assert await landed.column("t") == await source.texts("t")
        assert await landed.column("c5") == await source.scalars("rpad(c5::text, 5)")
        assert await landed.column("toString(u)") == await source.texts("u")
        assert await landed.column("toString(d)") == await source.texts("d")
        assert await landed.column("toString(ts)") == await source.texts("ts")
        assert await landed.column("toString(tz)") == await source.scalars(
            "to_char(tz at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')"
        )
        if clickhouse.major >= IPV4_IN_IPV6_SINCE:
            assert await landed.column("toString(ip)") == await source.scalars(
                "'::ffff:' || host(ip)"
            )
        else:
            assert await landed.column("ip") == await source.scalars("host(ip)")
        assert await landed.column("by") == await source.texts("by")
        assert await landed.column("iv") == await source.texts("iv")
        assert await landed.column("tr") == await source.texts("tr")
        assert await landed.column("bt") == await source.texts("bt")

    async def test_reload_passes_the_check(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "types",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
        )

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "types").count() == ROWS

    async def test_wider_table_takes_the_stream(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.wider")
        await clickhouse.command(
            f"create table {CH_DATABASE}.wider (id Int128, i4 Nullable(Int64), "
            "n Nullable(Decimal(38, 6)), tz Nullable(DateTime64(6, 'UTC')), "
            "vc LowCardinality(String)) engine = MergeTree order by id"
        )

        report = await land(
            postgres,
            clickhouse,
            "wider",
            ErrorIfNotExists(kind="error_if_not_exists"),
            select=f"select id, i4, n, tz, vc from {PG_SCHEMA}.src",
            columns=DECLARED[:2],
        )

        assert f"{ROWS} rows loaded" in report
        assert '"columns": "id"' in report
        assert "table Int128 is wider than stream Int64" in report
        assert await ChLoaded(clickhouse, "wider").count() == ROWS


class TestUnknownTypes:
    """Массив, numeric без точности, numeric(80, 2) и enum: у ClickHouse нет
    типа, который прочитает текст postgres без потерь."""

    SELECT: ClassVar[str] = f"select id, nfree, nwide, ia, en from {PG_SCHEMA}.src"

    async def test_refused_by_default(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        with pytest.raises(TransferError, match="column nfree: the target clickhouse"):
            await land(
                postgres,
                clickhouse,
                "unknown",
                DropAndCreate(kind="drop_and_create"),
                select=self.SELECT,
                columns=DECLARED[:1],
            )

    async def test_land_as_string_on_fallback(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "unknown",
            DropAndCreate(kind="drop_and_create"),
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
            select=self.SELECT,
            columns=DECLARED[:1],
        )
        landed = ChLoaded(clickhouse, "unknown")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "id": "Int64",
            "nfree": "Nullable(String)",
            "nwide": "Nullable(String)",
            "ia": "Nullable(String)",
            "en": "Nullable(String)",
        }
        assert await landed.column("ia") == await source.texts("ia")
        assert await landed.column("nwide") == await source.texts("nwide")
        assert await landed.column("en") == await source.texts("en")

    async def test_declared_type_wins(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "declared",
            DropAndCreate(kind="drop_and_create"),
            rules=ColumnRules(
                column_types={
                    "nfree": "Decimal(38, 4)",
                    "en": "Enum8('sad' = 1, 'happy' = 2)",
                }
            ),
            select=f"select id, nfree, en from {PG_SCHEMA}.src",
            columns=DECLARED[:1],
        )
        landed = ChLoaded(clickhouse, "declared")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "id": "Int64",
            "nfree": "Nullable(Decimal(38, 4))",
            "en": "Nullable(Enum8('sad' = 1, 'happy' = 2))",
        }
        assert await landed.column("toString(en)") == await Loaded(
            postgres, PG_SCHEMA, "src"
        ).texts("en")


class TestServerTraps:
    """Что ловит или молча меняет сам сервер ClickHouse при чтении текста
    postgres: маска в inet и массив на верхнем уровне json — ошибка загрузки,
    таблица не меняется; даты вне 1900–2299 прижимаются к границе."""

    async def test_masked_inet_fails_at_load(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        if clickhouse.major < IPV4_IN_IPV6_SINCE:
            pytest.skip("inet lands as String on this server")

        with pytest.raises(ClickHouseQueryError, match="Cannot parse"):
            await land(
                postgres,
                clickhouse,
                "masked",
                DropAndCreate(kind="drop_and_create"),
                select=f"select id, '10.0.0.0/8'::inet as ip from {PG_SCHEMA}.src",
                columns=DECLARED[:1],
            )

        assert await ChLoaded(clickhouse, "masked").count() == 0

    async def test_json_array_on_top_fails_at_load(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        with pytest.raises(ClickHouseQueryError):
            await land(
                postgres,
                clickhouse,
                "json_array",
                DropAndCreate(kind="drop_and_create"),
                select=f"select id, '[1, 2]'::jsonb as jb from {PG_SCHEMA}.src",
                columns=(DECLARED[0], DECLARED[2]),
            )

        assert await ChLoaded(clickhouse, "json_array").count() == 0

    async def test_dates_outside_the_range_are_clamped(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        await land(
            postgres,
            clickhouse,
            "clamped",
            DropAndCreate(kind="drop_and_create"),
            select=(
                f"select id, date '1800-06-15' as d, "
                f"timestamp '2500-01-01 00:00:00' as ts from {PG_SCHEMA}.src limit 1"
            ),
            columns=DECLARED[:1],
        )
        landed = ChLoaded(clickhouse, "clamped")

        assert await landed.column("toString(d)") == ["1900-01-01"]
        assert (await landed.column("toString(ts)"))[0].startswith("2299-12-31")


class TestMart:
    async def test_renamed_mart_with_declared_types(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await land(
            postgres,
            clickhouse,
            "mart",
            DropAndCreate(kind="drop_and_create"),
            rules=ColumnRules(
                rename_columns={"key": "id", "amount": "n", "address": "ip"},
                column_types={"amount": "Decimal(20, 4)", "address": "String"},
            ),
            select=f"select id, n, ip from {PG_SCHEMA}.src",
            columns=DECLARED[:1],
            order_by="key",
        )
        landed = ChLoaded(clickhouse, "mart")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "key": "Int64",
            "amount": "Nullable(Decimal(20, 4))",
            "address": "Nullable(String)",
        }
        assert await landed.column("address", order_by="key") == await source.scalars(
            "host(ip)"
        )
        assert Decimal(str(await landed.scalar("sum(amount)"))) == (
            await source.aggregate("sum(n)")
        )
