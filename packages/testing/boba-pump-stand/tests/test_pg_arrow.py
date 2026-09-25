"""Перекачка из PostgreSQL потоком Arrow IPC: pg_arrow_out против pg_arrow_in
(круг на каждом postgres и Greenplum из sources), ch_arrow_in (в каждый
ClickHouse из ch_sources) и ora_arrow_in (в каждый Oracle из ora_sources).
Насосы соединены трубой ОС и работают одновременно.

Таблица postgres несёт все семейства типов: целые, numeric с точностью и без,
float с NaN и бесконечностями, boolean, text со спецсимволами и char, bytea,
date, timestamp, timestamptz, time, interval, uuid, массивы, json/jsonb,
inet, enum, композит, диапазон, money, bit, геометрию и xml. У каждой
колонки записано, каким типом она едет в Arrow (описание стейтмента у libpq
без выполнения), что писать в select под конкретный приёмник и как
сверяется; всё, чего CSV не несёт (bytea, массивы, time, interval, uuid,
json, составные), едет текстом сервера — по каталогу видно, какие типы едут
только строкой. Отдельные тесты фиксируют ловушки: numeric без точности и
шире 38 знаков отвергаются до выполнения, списки в потоке — до загрузки,
float печатается точно на любой версии сервера, сессия COPY не зависит от
настроек профиля."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar

import pytest

from boba.db.postgres import PgArrowError
from boba.pump_stand import (
    ClickHouseSide,
    Leg,
    OracleSide,
    PgSource,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.pump_stand.compare import (
    BYTES,
    DATETIME,
    EXACT,
    FLOAT,
    FLOAT32,
    JSON,
    NUMBER,
    Values,
)
from boba.pump_stand.matrix import Target, compared, exported, first

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()


def _newest_postgres(sources: Sequence[PgSource]) -> str:
    """Имя новейшего PostgreSQL стенда (Greenplum не в счёт)."""
    plain: list[str] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source.name)

    return plain[-1]


NEWEST = _newest_postgres(STAND.sources)
ROWS = 2000
ARRAYSIZE = 97
CHUNK_BYTES = 4096
PG_SCHEMA = "pump_pg_arrow"
CH_DATABASE = "pump_pg_arrow"
NULL_EVERY = 7


@dataclass(frozen=True)
class PgColumn:
    """Колонка postgres: тип, заполнение над g, минимальная версия сервера,
    сверка, тип в Arrow (как печатает pyarrow) и стороны приёмников."""

    name: str
    pg_type: str
    fill: str
    compare: Values
    arrow: str
    pg: Target
    ch: Target | None
    ora: Target | None
    nullable: bool = False
    min_version: int = 0
    unicode: bool = False

    def filled(self) -> str:
        if not self.nullable:
            return f"({self.fill})"

        return f"case when g % {NULL_EVERY} = 0 then null else ({self.fill}) end"


NFREE = "nfree::numeric(30,12)"
TZ_UTC = "tz at time zone 'UTC'"

PG_COLUMNS = (
    PgColumn(
        "id",
        "bigint",
        "g",
        NUMBER,
        "int64",
        Target("bigint"),
        Target("Int64"),
        Target("number(19)"),
    ),
    PgColumn(
        "i2",
        "smallint",
        "((g % 30000) - 15000)::smallint",
        NUMBER,
        "int16",
        Target("smallint"),
        Target("Int16"),
        Target("number(5)"),
        nullable=True,
    ),
    PgColumn(
        "i4",
        "integer",
        "((g % 3000) * 700000 - 2147483648)::int",
        NUMBER,
        "int32",
        Target("integer"),
        Target("Int32"),
        Target("number(10)"),
    ),
    PgColumn(
        "n18",
        "numeric(18,6)",
        "g::numeric / 7 - 1000",
        NUMBER,
        "decimal128(18, 6)",
        Target("numeric(18,6)"),
        Target("Decimal(18, 6)"),
        Target("number(18,6)"),
        nullable=True,
    ),
    PgColumn(
        "n38",
        "numeric(38,10)",
        "g::numeric * 1234567890.123456789 - 9999999999999999999999999.9876543210",
        NUMBER,
        "decimal128(38, 10)",
        Target("numeric(38,10)"),
        Target("Decimal(38, 10)"),
        Target("number(38,10)"),
    ),
    PgColumn(
        "nfree",
        "numeric",
        "g::numeric / 7",
        NUMBER,
        "decimal128(30, 12)",
        Target("numeric", out=NFREE, src_ref=NFREE),
        Target("Decimal(30, 12)", out=NFREE, src_ref=NFREE),
        Target("number(30,12)", out=NFREE, src_ref=NFREE),
        nullable=True,
    ),
    PgColumn(
        "r4",
        "real",
        "case g % 5 when 0 then 'NaN'::real when 1 then 'Infinity' "
        "when 2 then '-Infinity' else (g::real / 3) end",
        FLOAT32,
        "float",
        Target("real"),
        Target("Float32"),
        Target("binary_float"),
    ),
    PgColumn(
        "r8",
        "double precision",
        "case g % 5 when 0 then 'NaN'::float8 when 1 then 'Infinity' "
        "when 2 then '-0' else g::float8 / 7 end",
        FLOAT,
        "double",
        Target("double precision"),
        Target("Float64"),
        Target("binary_double"),
        nullable=True,
    ),
    PgColumn(
        "b",
        "boolean",
        "g % 2 = 0",
        NUMBER,
        "bool",
        Target("boolean"),
        Target("Bool"),
        Target("number(1)", out="b::int"),
        nullable=True,
    ),
    PgColumn(
        "t",
        "text",
        "'tab\\t' || g || E'\\nnew\\\\line ''q'' \"dq\", ко 中文 🙂'",
        EXACT,
        "large_string",
        Target("text"),
        Target("String"),
        Target("nvarchar2(400)"),
        nullable=True,
        unicode=True,
    ),
    PgColumn(
        "c5",
        "char(5)",
        "'ab'",
        EXACT,
        "large_string",
        Target("char(5)"),
        Target("String"),
        Target("char(5)"),
    ),
    PgColumn(
        "bin",
        "bytea",
        "decode(md5(g::text), 'hex') || E'\\\\000\\\\377'::bytea",
        BYTES,
        "large_string",
        Target("bytea"),
        Target("String", src_ref="bin::text"),
        Target("raw(100)", out="encode(bin, 'hex')", src_ref="encode(bin, 'hex')"),
        nullable=True,
    ),
    PgColumn(
        "d",
        "date",
        "date '1900-01-01' + (g % 140000)",
        DATETIME,
        "date32[day]",
        Target("date"),
        Target("Date32"),
        Target("date"),
    ),
    PgColumn(
        "ts",
        "timestamp(6)",
        "timestamp '2000-01-01 00:00:00.123456' "
        "+ g * interval '1 hour 1 minute 1.000001 second'",
        DATETIME,
        "timestamp[us]",
        Target("timestamp(6)"),
        Target("DateTime64(6, 'UTC')"),
        Target("timestamp(6)"),
        nullable=True,
    ),
    PgColumn(
        "tz",
        "timestamptz(6)",
        "timestamptz '2000-01-01 00:00:00.5+03' + g * interval '1 day'",
        DATETIME,
        "timestamp[us, tz=UTC]",
        Target("timestamptz(6)"),
        Target("DateTime64(6, 'UTC')"),
        Target("timestamp(6)", out=TZ_UTC, src_ref=TZ_UTC),
    ),
    PgColumn(
        "tm",
        "time",
        "time '00:00:00' + g * interval '1 second'",
        EXACT,
        "large_string",
        Target("time"),
        Target("String", out="tm::text", src_ref="tm::text"),
        Target("varchar2(20)", out="tm::text", src_ref="tm::text"),
    ),
    PgColumn(
        "iv",
        "interval",
        "g * interval '1 day 2 hours 3 minutes'",
        EXACT,
        "large_string",
        Target("interval", src_ref="iv::text"),
        Target("String", src_ref="iv::text"),
        Target("varchar2(40)", src_ref="iv::text"),
        nullable=True,
    ),
    PgColumn(
        "u",
        "uuid",
        "md5(g::text)::uuid",
        EXACT,
        "large_string",
        Target("uuid", src_ref="u::text"),
        Target("UUID", ref="toString(u)", src_ref="u::text"),
        Target("varchar2(36)", src_ref="u::text"),
        nullable=True,
    ),
    PgColumn(
        "ai",
        "int[]",
        "array[g, g + 1, null]",
        EXACT,
        "large_string",
        Target("int[]"),
        Target("String", src_ref="ai::text"),
        Target("varchar2(100)", src_ref="ai::text"),
    ),
    PgColumn(
        "at",
        "text[]",
        "array['a,b', 'c\"d', '{e}']",
        EXACT,
        "large_string",
        Target("text[]"),
        Target("String", src_ref="at::text"),
        Target("varchar2(100)", src_ref="at::text"),
    ),
    PgColumn(
        "an",
        "numeric(18,4)[]",
        "array[g::numeric / 3, 1.5]",
        EXACT,
        "large_string",
        Target("numeric(18,4)[]"),
        Target("String", src_ref="an::text"),
        Target("varchar2(100)", src_ref="an::text"),
    ),
    PgColumn(
        "js",
        "jsonb",
        '(\'{"a": \' || g || \', "b": {"c": null}}\')::jsonb',
        JSON,
        "large_string",
        Target("jsonb", src_ref="js::text"),
        Target("String", src_ref="js::text"),
        Target("clob check (js is json)", src_ref="js::text"),
        nullable=True,
        min_version=90400,
    ),
    PgColumn(
        "ip",
        "inet",
        "('10.0.' || (g % 256) || '.1')::inet",
        EXACT,
        "large_string",
        Target("inet", src_ref="host(ip)"),
        Target("IPv4", ref="toString(ip)", src_ref="host(ip)"),
        Target("varchar2(20)", src_ref="host(ip)"),
    ),
    PgColumn(
        "mood",
        "mood",
        "(array['sad', 'ok', 'happy'])[g % 3 + 1]::mood",
        EXACT,
        "large_string",
        Target("mood", src_ref="mood::text"),
        Target("String", src_ref="mood::text"),
        Target("varchar2(10)", src_ref="mood::text"),
        nullable=True,
    ),
    PgColumn(
        "comp",
        "pair",
        "row(g, 'x,y')::pair",
        EXACT,
        "large_string",
        Target("pair", src_ref="comp::text"),
        Target("String", src_ref="comp::text"),
        Target("varchar2(40)", src_ref="comp::text"),
    ),
    PgColumn(
        "r4r",
        "int4range",
        "int4range(g, g + 10)",
        EXACT,
        "large_string",
        Target("int4range", src_ref="r4r::text"),
        Target("String", src_ref="r4r::text"),
        Target("varchar2(40)", src_ref="r4r::text"),
        min_version=90200,
    ),
    PgColumn(
        "mo",
        "money",
        "((g::numeric / 100)::text)::money",
        EXACT,
        "large_string",
        Target("money", src_ref="mo::text"),
        Target("String", src_ref="mo::text"),
        Target("varchar2(40)", src_ref="mo::text"),
    ),
    PgColumn(
        "bt",
        "bit(4)",
        "B'1010'",
        EXACT,
        "large_string",
        Target("bit(4)", src_ref="bt::text"),
        Target("String", src_ref="bt::text"),
        Target("varchar2(4)", src_ref="bt::text"),
    ),
    PgColumn(
        "pt",
        "point",
        "point(g, g / 2.0)",
        EXACT,
        "large_string",
        Target("point", src_ref="pt::text"),
        Target("String", src_ref="pt::text"),
        Target("varchar2(40)", src_ref="pt::text"),
        nullable=True,
    ),
    PgColumn(
        "xm",
        "xml",
        "('<r id=\"' || g || '\"><v>x&amp;y</v></r>')::xml",
        EXACT,
        "large_string",
        Target("xml", src_ref="xm::text"),
        Target("String", src_ref="xm::text"),
        Target("varchar2(200)", src_ref="xm::text"),
    ),
)


class Source:
    """Таблицы src и dst в схеме postgres под версию сервера."""

    def __init__(self, side: PostgresSide) -> None:
        self.side = side

    def columns(self) -> list[PgColumn]:
        chosen: list[PgColumn] = []
        for column in PG_COLUMNS:
            if column.min_version <= self.side.version:
                chosen.append(column)

        return chosen

    async def fill(self, rows: int) -> None:
        columns = self.columns()
        ddl = ", ".join(f"{c.name} {c.pg_type}" for c in columns)
        filled = ", ".join(f"{c.filled()} as {c.name}" for c in columns)
        await self.side.recreate_schema(
            (
                f"create type {PG_SCHEMA}.mood as enum ('sad', 'ok', 'happy')",
                f"create type {PG_SCHEMA}.pair as (a int, b text)",
                f"create table {PG_SCHEMA}.src ({ddl})",
                f"create table {PG_SCHEMA}.dst ({ddl})",
                f"insert into {PG_SCHEMA}.src select {filled} "
                f"from generate_series(1, {rows}) g",
            )
        )


def _table_name(source_name: str) -> str:
    return "from_" + source_name.replace("-", "_").replace(".", "_")


def _names(columns: Sequence[PgColumn]) -> list[str]:
    return [c.name for c in columns]


def _select(columns: Sequence[PgColumn], targets: Sequence[Target]) -> str:
    listed = exported(_names(columns), targets, False)

    return f"select {listed} from {PG_SCHEMA}.src order by id"


async def _report(
    source: Source, columns: Sequence[PgColumn], targets: Sequence[Target], landed: Any
) -> Any:
    expected = await source.side.select(
        "src", [first(t.src_ref, c.name) for c, t in zip(columns, targets, strict=True)]
    )

    return compared(
        _names(columns),
        [c.compare for c in columns],
        [t.approx for t in targets],
        expected,
        landed,
    )


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await Source(side).fill(ROWS)
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    await side.recreate_database()
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[OracleSide]:
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


class TestPostgresToPostgres:
    async def test_every_type_survives_the_circle(self, postgres: PostgresSide) -> None:
        source = Source(postgres)
        columns = source.columns()
        targets = [c.pg for c in columns]
        pumps = Pumps(postgres=postgres.profile)

        chained = await pumps.chain(
            Leg(
                "pg_arrow_out",
                {"sql": _select(columns, targets), "chunk_bytes": CHUNK_BYTES},
            ),
            Leg(
                "pg_arrow_in", {"table": f"{PG_SCHEMA}.dst", "chunk_bytes": CHUNK_BYTES}
            ),
        )
        assert chained.in_report == f"{ROWS} rows written into {PG_SCHEMA}.dst"

        # обе стороны postgres: опорное выражение источника годится и приёмнику
        refs = [first(t.src_ref, c.name) for c, t in zip(columns, targets, strict=True)]
        landed = await postgres.select("dst", refs)
        report = await _report(source, columns, targets, landed)

        assert not report.mismatches, report.render()


class TestPostgresToClickHouse:
    async def test_every_type_lands(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        source = Source(postgres)
        columns: list[PgColumn] = []
        targets: list[Target] = []
        for column in source.columns():
            if column.ch is None:
                continue

            columns.append(column)
            targets.append(column.ch)

        parts: list[str] = []
        for column, target in zip(columns, targets, strict=True):
            ch_type = target.type
            if column.nullable:
                ch_type = f"Nullable({ch_type})"

            parts.append(f"{column.name} {ch_type}")

        table = _table_name(postgres.source.name)
        await clickhouse.create(table, parts)
        pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.profile)
        chained = await pumps.chain(
            Leg(
                "pg_arrow_out",
                {"sql": _select(columns, targets), "chunk_bytes": CHUNK_BYTES},
            ),
            Leg(
                "ch_arrow_in",
                {
                    "sql": f"insert into {CH_DATABASE}.{table} format ArrowStream",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
        )
        assert chained.in_report == f"{ROWS} rows written"

        landed = await clickhouse.select(
            table, [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)]
        )
        report = await _report(source, columns, targets, landed)

        assert not report.mismatches, report.render()


class TestPostgresToOracle:
    LOB: ClassVar[str] = "clob"

    async def test_every_type_lands(
        self, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        """LOB-колонки приёмника идут последними: Oracle не принимает длинный
        bind после LOB в одном insert (ORA-24816), а порядок bind'ов — порядок
        полей схемы. Текст вне ASCII в базу с однобайтовой кодировкой не
        доезжает даже в nvarchar2: bind проходит через кодировку базы."""
        source = Source(postgres)
        columns: list[PgColumn] = []
        lobs: list[PgColumn] = []
        for column in source.columns():
            if column.ora is None:
                continue

            if column.unicode and not oracle.unicode:
                continue

            if self.LOB in column.ora.type:
                lobs.append(column)
                continue

            columns.append(column)

        columns.extend(lobs)
        targets: list[Target] = []
        for column in columns:
            if column.ora is not None:
                targets.append(column.ora)

        table = _table_name(postgres.source.name)
        await oracle.create(
            table, [f"{c.name} {t.type}" for c, t in zip(columns, targets, strict=True)]
        )
        pumps = Pumps(postgres=postgres.profile, oracle=oracle.profile)
        try:
            chained = await pumps.chain(
                Leg(
                    "pg_arrow_out",
                    {"sql": _select(columns, targets), "chunk_bytes": CHUNK_BYTES},
                ),
                Leg("ora_arrow_in", {"table": table, "chunk_bytes": CHUNK_BYTES}),
            )
            assert chained.in_report == f"{ROWS} rows written into {table}"

            landed = await oracle.select(
                table,
                [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)],
            )
        finally:
            await oracle.drop_table(table)

        report = await _report(source, columns, targets, landed)

        assert not report.mismatches, report.render()


class TestTraps:
    """Ловушки Arrow-пути postgres на новейшем сервере."""

    SLOW: ClassVar[str] = "select pg_sleep(30), 1::numeric as n"

    async def test_unbounded_numeric_is_refused_before_execution(
        self, postgres: PostgresSide
    ) -> None:
        """numeric без точности отвергается по описанию стейтмента: запрос с
        pg_sleep(30) не выполняется, ответ приходит сразу."""
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        pumps = Pumps(postgres=postgres.profile)
        started = time.monotonic()
        with pytest.raises(PgArrowError, match="numeric without precision"):
            await pumps.chain(
                Leg("pg_arrow_out", {"sql": self.SLOW, "chunk_bytes": CHUNK_BYTES}),
                Leg(
                    "pg_arrow_in",
                    {"table": f"{PG_SCHEMA}.dst", "chunk_bytes": CHUNK_BYTES},
                ),
            )

        assert time.monotonic() - started < 5

    async def test_wide_numeric_needs_text(self, postgres: PostgresSide) -> None:
        """numeric шире 38 знаков читатель CSV Arrow не собирает: отказ до
        выполнения с подсказкой ::text."""
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        pumps = Pumps(postgres=postgres.profile)
        with pytest.raises(PgArrowError, match="up to 38 digits"):
            await pumps.chain(
                Leg(
                    "pg_arrow_out",
                    {
                        "sql": "select pg_sleep(30), 1::numeric(50, 20) as n",
                        "chunk_bytes": CHUNK_BYTES,
                    },
                ),
                Leg(
                    "pg_arrow_in",
                    {"table": f"{PG_SCHEMA}.dst", "chunk_bytes": CHUNK_BYTES},
                ),
            )

    async def test_broken_statement_is_refused_by_describe(
        self, postgres: PostgresSide
    ) -> None:
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        pumps = Pumps(postgres=postgres.profile)
        with pytest.raises(PgArrowError, match="the statement on postgres failed"):
            await pumps.chain(
                Leg(
                    "pg_arrow_out",
                    {"sql": "select nothing from nowhere", "chunk_bytes": CHUNK_BYTES},
                ),
                Leg(
                    "pg_arrow_in",
                    {"table": f"{PG_SCHEMA}.dst", "chunk_bytes": CHUNK_BYTES},
                ),
            )

    async def test_arrays_travel_as_text(self, postgres: PostgresSide) -> None:
        """Массив любой размерности едет текстом postgres и ложится в колонку
        массива как есть."""
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        await postgres.create("dims", ["id bigint", "a2 int[][]"])
        pumps = Pumps(postgres=postgres.profile)
        await pumps.chain(
            Leg(
                "pg_arrow_out",
                {
                    "sql": "select 1::bigint as id, "
                    "array[array[1, 2], array[3, 4]] as a2",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
            Leg(
                "pg_arrow_in",
                {"table": f"{PG_SCHEMA}.dims", "chunk_bytes": CHUNK_BYTES},
            ),
        )
        landed = await postgres.select("dims", ["id", "a2"])

        assert landed == [(1, [[1, 2], [3, 4]])]

    async def test_row_wider_than_the_parse_block_needs_bigger_chunks(
        self, postgres: PostgresSide
    ) -> None:
        """Строка CSV обязана уместиться в один блок читателя: при chunk_bytes
        ниже пола блок — 1 MiB, строка в 3 MiB отвергается с подсказкой, а
        chunk_bytes в 4 MiB её проносит."""
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        await postgres.create("wide", ["id bigint", "t text"])
        pumps = Pumps(postgres=postgres.profile)
        wide = "select 1::bigint as id, repeat('x', 3 * 1024 * 1024) as t"
        with pytest.raises(PgArrowError, match="raise chunk_bytes"):
            await pumps.chain(
                Leg("pg_arrow_out", {"sql": wide, "chunk_bytes": CHUNK_BYTES}),
                Leg(
                    "pg_arrow_in",
                    {"table": f"{PG_SCHEMA}.wide", "chunk_bytes": CHUNK_BYTES},
                ),
            )

        await pumps.chain(
            Leg("pg_arrow_out", {"sql": wide, "chunk_bytes": 4 * 1024 * 1024}),
            Leg(
                "pg_arrow_in",
                {"table": f"{PG_SCHEMA}.wide", "chunk_bytes": 4 * 1024 * 1024},
            ),
        )
        landed = await postgres.select("wide", ["id", "length(t)"])

        assert landed == [(1, 3 * 1024 * 1024)]

    async def test_list_in_the_stream_is_refused_before_loading(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        """Список Arrow (Array ClickHouse) CSV не несёт: pg_arrow_in отвергает
        его по схеме, источник обязан отдать текст postgres."""
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        await postgres.create("lists", ["id bigint", "arr bigint[]"])
        pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.profile)
        with pytest.raises(PgArrowError, match="cannot be written as csv"):
            await pumps.chain(
                Leg(
                    "ch_arrow_out",
                    {
                        "sql": "select toInt64(1) as id, [toInt64(1), 2] as arr",
                        "chunk_bytes": CHUNK_BYTES,
                    },
                ),
                Leg(
                    "pg_arrow_in",
                    {"table": f"{PG_SCHEMA}.lists", "chunk_bytes": CHUNK_BYTES},
                ),
            )

    async def test_copy_session_is_fixed_regardless_of_profile_options(
        self, postgres: PostgresSide
    ) -> None:
        """Текст COPY не зависит от настроек сессии профиля: даты ISO, UTC, bytea
        hex, интервал в записи postgres, money без локали, float точно — и у
        pg_stream_out, и у pg_arrow_out."""
        if postgres.source.name != NEWEST:
            pytest.skip("one postgres is enough for this trap")

        odd = postgres.profile.options.model_copy(
            update={
                "datestyle": "Postgres, DMY",
                "intervalstyle": "sql_standard",
                "timezone": "Europe/Moscow",
                "bytea_output": "escape",
                "lc_monetary": "ru_RU.UTF-8",
                "extra_float_digits": "0",
            }
        )
        profile = postgres.profile.model_copy(update={"options": odd})
        select = (
            "select 1::bigint as id, 1.0::float8 / 3 as f, "
            "timestamptz '2024-02-29 13:14:15.123456+03' as tz, "
            "date '2024-02-29' as d, interval '1 day 2 hours 3.5 seconds' as iv, "
            "'\\x00ff'::bytea as b, 12.5::money as m"
        )
        pumps = Pumps(postgres=profile)

        text = await pumps.pg_out(f"copy ({select}) to stdout (format csv)")
        await postgres.create(
            "session",
            [
                "id bigint",
                "f double precision",
                "tz timestamptz",
                "d date",
                "iv text",
                "b bytea",
                "m text",
            ],
        )
        await pumps.chain(
            Leg("pg_arrow_out", {"sql": select, "chunk_bytes": CHUNK_BYTES}),
            Leg(
                "pg_arrow_in",
                {"table": f"{PG_SCHEMA}.session", "chunk_bytes": CHUNK_BYTES},
            ),
        )
        landed = await postgres.select(
            "session",
            ["f", "tz at time zone 'UTC'", "d", "iv", "encode(b, 'hex')", "m"],
        )

        assert text == (
            b"1,0.3333333333333333,2024-02-29 10:14:15.123456+00,2024-02-29,"
            b"1 day 02:00:03.5,\\x00ff,$12.50\n"
        )
        assert landed[0][1:] == (
            datetime(2024, 2, 29, 10, 14, 15, 123456),
            date(2024, 2, 29),
            "1 day 02:00:03.5",
            "00ff",
            "$12.50",
        )
        assert landed[0][0] == 1.0 / 3

    async def test_float_is_exact_on_every_server(self, postgres: PostgresSide) -> None:
        """extra_float_digits = 3 в опциях сессии выгрузки: double едет точно
        и на серверах до 12-й версии, где обычная печать даёт 15 знаков."""
        await postgres.create("floats", ["id bigint", "r8 double precision"])
        pumps = Pumps(postgres=postgres.profile)
        await pumps.chain(
            Leg(
                "pg_arrow_out",
                {
                    "sql": "select g::bigint as id, g::float8 / 7 as r8 "
                    "from generate_series(1, 1000) g",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
            Leg(
                "pg_arrow_in",
                {"table": f"{PG_SCHEMA}.floats", "chunk_bytes": CHUNK_BYTES},
            ),
        )
        landed = await postgres.select("floats", ["id", "r8"])

        assert [row[1] for row in landed] == [g / 7 for g in range(1, 1001)]
