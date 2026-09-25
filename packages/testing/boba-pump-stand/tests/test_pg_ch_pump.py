"""Перекачка между postgres и ClickHouse насосами pg_stream_out/pg_stream_in и
ch_stream_out/ch_stream_in на всей матрице стенда: каждый postgres и Greenplum
из pg_sources против каждого ClickHouse из ch_sources. Стейтменты пишутся
целиком, как их писала бы LLM, байты идут между узлами без разбора.

Ветка postgres -> ClickHouse -> postgres везёт все семейства типов postgres:
целые, numeric, float с NaN и Infinity, text со спецсимволами, bytea, даты и
время, uuid, массивы (в том числе двумерные), геометрию, сетевые типы, enum,
композит, bit, money, json/jsonb и диапазоны там, где сервер их знает. Ветка
ClickHouse -> postgres -> ClickHouse везёт типы ClickHouse: целые до 256 бит,
Decimal до 256 бит, Float с NaN, String и FixedString с NUL, LowCardinality,
Date32, DateTime64 до наносекунд, Enum, UUID, IPv4/IPv6, Bool, вложенные
массивы, Tuple, Map, гео-типы и Nullable; составные значения едут в postgres
как JSON и возвращаются через input() с JSONExtract. Дампы источника и
приёмника сравниваются байт в байт. Допуск есть только у float: postgres
до 12-й версии печатает real и double с 6 и 15 значащими цифрами, а
ClickHouse без precise_float_parsing (22.x) при текстовом разборе теряет
один ulp; в этих случаях float сравниваются с относительной точностью, а
на новых серверах с обеих сторон — байт в байт."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.postgres import AsyncPostgresPool
from boba.pump_stand import ChSource, PgSource, Pumps, PumpStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROWS = 2000
BIG_ROWS = 300_000
BIG_CHUNK = 1 << 16
CHUNK = 777
PG_SCHEMA = "pump_stand"
CH_DATABASE = "pump_stand"
SHORTEST_FLOAT_OUTPUT = 120000
"""Версия postgres, с которой float печатаются кратчайшим точным текстом."""


STAND = PumpStand.required()


@dataclass(frozen=True)
class PgColumn:
    """Колонка postgres: тип, выражение над g (номер строки), тип в
    ClickHouse на промежуточной таблице, минимальная версия сервера."""

    name: str
    pg_type: str
    expression: str
    ch_type: str
    nullable: bool = False
    min_version: int = 0


PG_COLUMNS = (
    PgColumn("id", "bigint", "g", "Int64"),
    PgColumn("i2", "smallint", "((g % 30000) - 15000)::smallint", "Int16", True),
    PgColumn("i4", "integer", "((g % 3000) * 700000 - 2147483648)::int", "Int32"),
    PgColumn(
        "i8",
        "bigint",
        "(g % 3000) * 3002399751580331 - 4611686018427387904",
        "Int64",
    ),
    PgColumn(
        "n38",
        "numeric(38,10)",
        "g::numeric * 1234567890.123456789 - 9999999999999999999999999.9876543210",
        "Decimal(38,10)",
        True,
    ),
    PgColumn("nfree", "numeric", "g::numeric / 7", "String"),
    PgColumn(
        "r4",
        "real",
        "case g % 5 when 0 then 'NaN'::real when 1 then 'Infinity' "
        "when 2 then '-Infinity' else (g::real / 3) end",
        "Float32",
    ),
    PgColumn(
        "r8",
        "double precision",
        "case g % 5 when 0 then 'NaN'::float8 when 1 then 'Infinity' "
        "when 2 then '-0' else g::float8 / 7 end",
        "Float64",
        True,
    ),
    PgColumn("b", "boolean", "g % 2 = 0", "Bool"),
    PgColumn(
        "t",
        "text",
        "'tab\\t' || g || E'\\nnew\\\\line ''q'' \"dq\" ко 中文 🙂'",
        "String",
        True,
    ),
    PgColumn("c5", "char(5)", "'ab'", "String"),
    PgColumn("v", "varchar(20)", "'v' || g", "String"),
    PgColumn(
        "by",
        "bytea",
        "decode(md5(g::text), 'hex') || E'\\\\000\\\\377'::bytea",
        "String",
        True,
    ),
    PgColumn("d", "date", "date '1900-01-01' + (g % 140000)", "Date32"),
    PgColumn(
        "ts",
        "timestamp(6)",
        "timestamp '2000-01-01 00:00:00.123456' "
        "+ g * interval '1 hour 1 minute 1.000001 second'",
        "DateTime64(6)",
        True,
    ),
    PgColumn(
        "tz",
        "timestamptz(6)",
        "timestamptz '2000-01-01 00:00:00.5+03' + g * interval '1 day'",
        "String",
    ),
    PgColumn("tm", "time", "time '00:00:00' + g * interval '1 second'", "String"),
    PgColumn("iv", "interval", "g * interval '1 day 2 hours 3 minutes'", "String"),
    PgColumn("u", "uuid", "md5(g::text)::uuid", "UUID", True),
    PgColumn("ai", "int[]", "array[g, g + 1, null]", "String", True),
    PgColumn("at", "text[]", "array['a,b', 'c\"d', '{e}', null]", "String"),
    PgColumn("an", "numeric[]", "array[g::numeric / 3, 1.5]", "String"),
    PgColumn("a2", "int[][]", "array[array[g, 1], array[2, 3]]", "String"),
    PgColumn("pt", "point", "point(g, g / 2.0)", "String", True),
    PgColumn("poly", "polygon", "polygon '((0,0),(1,0),(1,1))'", "String"),
    PgColumn("bx", "box", "box '((0,0),(1,1))'", "String"),
    PgColumn("pth", "path", "path '[(0,0),(1,1)]'", "String"),
    PgColumn("ip", "inet", "('10.0.' || (g % 256) || '.1')::inet", "String"),
    PgColumn("cd", "cidr", "'10.0.0.0/8'::cidr", "String"),
    PgColumn("mac", "macaddr", "'08:00:2b:01:02:03'::macaddr", "String"),
    PgColumn(
        "mood", "mood", "(array['sad', 'ok', 'happy'])[g % 3 + 1]::mood", "String", True
    ),
    PgColumn("comp", "pair", "row(g, 'x,y')::pair", "String", True),
    PgColumn("bt", "bit(4)", "B'1010'", "String"),
    PgColumn("vb", "varbit", "B'101'", "String"),
    PgColumn("mo", "money", "((g::numeric / 100)::text)::money", "String"),
    PgColumn(
        "js",
        "json",
        '(\'{"a": \' || g || \', "b": [1, "x"]}\')::json',
        "String",
        min_version=90200,
    ),
    PgColumn(
        "jb",
        "jsonb",
        '(\'{"a": \' || g || \', "b": {"c": null}}\')::jsonb',
        "String",
        True,
        90400,
    ),
    PgColumn("r4r", "int4range", "int4range(g, g + 10)", "String", min_version=90200),
    PgColumn(
        "tsr",
        "tstzrange",
        "tstzrange('2020-01-01', '2020-02-01')",
        "String",
        True,
        90200,
    ),
)
PG_FLOATS = ("r4", "r8")


@dataclass(frozen=True)
class FloatTolerance:
    """Относительный допуск на float после круга: у postgres до 12-й версии
    его задаёт печать в 6 и 15 значащих цифр, у ClickHouse без точного
    разбора — один ulp; иначе допуска нет и float сравниваются байт в байт."""

    pg_version: int
    precise_parsing: bool

    def of(self, name: str) -> str | None:
        single = name in ("r4", "f32")
        if self.pg_version < SHORTEST_FLOAT_OUTPUT:
            if single:
                return "1e-5"

            return "1e-14"

        if self.precise_parsing:
            return None

        if single:
            return "2e-7"

        return "4e-16"

    def exact(self, names: Sequence[str]) -> list[str]:
        chosen: list[str] = []
        for name in names:
            if self.of(name) is None:
                chosen.append(name)

        return chosen

    def loose(self, names: Sequence[str]) -> dict[str, str]:
        chosen: dict[str, str] = {}
        for name in names:
            tolerance = self.of(name)
            if tolerance is not None:
                chosen[name] = tolerance

        return chosen


@dataclass(frozen=True)
class ChColumn:
    """Колонка ClickHouse: тип, выражение над n (номер строки), тип в postgres
    на промежуточной таблице, как уезжает в postgres и как возвращается
    через input()."""

    name: str
    ch_type: str
    expression: str
    pg_type: str
    outbound: str
    input_type: str
    inbound: str


JSON_TYPE = "JSONB"
CH_COLUMNS = (
    ChColumn("id", "UInt64", "n", "numeric", "id", "UInt64", "id"),
    ChColumn("i8", "Int8", "toInt8(n % 256 - 128)", "smallint", "i8", "Int8", "i8"),
    ChColumn("u8", "UInt8", "toUInt8(n % 256)", "smallint", "u8", "UInt8", "u8"),
    ChColumn(
        "i16", "Int16", "toInt16(n * 21 - 32768)", "integer", "i16", "Int16", "i16"
    ),
    ChColumn("u16", "UInt16", "toUInt16(n * 21)", "integer", "u16", "UInt16", "u16"),
    ChColumn(
        "i32",
        "Int32",
        "toInt32(n * 700000 - 2147483648)",
        "bigint",
        "i32",
        "Int32",
        "i32",
    ),
    ChColumn(
        "u32", "UInt32", "toUInt32(n * 1400000)", "bigint", "u32", "UInt32", "u32"
    ),
    ChColumn(
        "i64",
        "Int64",
        "toInt64(n * 3002399751580331 - 4611686018427387904)",
        "numeric",
        "i64",
        "Int64",
        "i64",
    ),
    ChColumn(
        "u64",
        "UInt64",
        "toUInt64(18446744073709551615 - n)",
        "numeric",
        "u64",
        "UInt64",
        "u64",
    ),
    ChColumn(
        "i128",
        "Int128",
        "toInt128('-170141183460469231731687303715884105728') + toInt128(n)",
        "numeric",
        "i128",
        "Int128",
        "i128",
    ),
    ChColumn(
        "u256",
        "UInt256",
        "toUInt256('1157920892373161954235709850086879078532699846656405640394575840"
        "07913129639935') - toUInt256(n)",
        "numeric",
        "u256",
        "UInt256",
        "u256",
    ),
    ChColumn(
        "f32", "Float32", "toFloat32(n) / 3", "real", "f32", "String", "toFloat32(f32)"
    ),
    ChColumn(
        "f64",
        "Float64",
        "multiIf(n % 5 = 0, toFloat64('nan'), n % 5 = 1, toFloat64('inf'), "
        "n % 5 = 2, toFloat64('-inf'), toFloat64(n) / 7)",
        "double precision",
        "f64",
        "String",
        "toFloat64(f64)",
    ),
    ChColumn(
        "d32",
        "Decimal32(4)",
        "toDecimal32(n, 4) / 3",
        "numeric(9,4)",
        "d32",
        "Decimal32(4)",
        "d32",
    ),
    ChColumn(
        "d64",
        "Decimal64(8)",
        "toDecimal64(n, 8) / 7",
        "numeric(18,8)",
        "d64",
        "Decimal64(8)",
        "d64",
    ),
    ChColumn(
        "d128",
        "Decimal128(20)",
        "toDecimal128(n, 20) / 11",
        "numeric(38,20)",
        "d128",
        "Decimal128(20)",
        "d128",
    ),
    ChColumn(
        "d256",
        "Decimal256(40)",
        "toDecimal256(n, 40) / 13",
        "numeric(76,40)",
        "d256",
        "Decimal256(40)",
        "d256",
    ),
    ChColumn(
        "s",
        "String",
        "concat('tab\\t', toString(n), '\\nnew\\\\line ''q'' \"dq\" ко 中文 🙂')",
        "text",
        "s",
        "String",
        "s",
    ),
    ChColumn(
        "fs",
        "FixedString(4)",
        "toFixedString(concat('a', char(0), 'b'), 4)",
        "bytea",
        "concat('\\\\x', hex(fs))",
        "String",
        "unhex(substring(fs, 3))",
    ),
    ChColumn(
        "lc",
        "LowCardinality(String)",
        "toLowCardinality(toString(n % 5))",
        "text",
        "lc",
        "LowCardinality(String)",
        "lc",
    ),
    ChColumn("dt", "Date", "toDate('1970-01-01') + n", "date", "dt", "Date", "dt"),
    ChColumn(
        "d32_",
        "Date32",
        "toDate32('1900-01-01') + n * 20",
        "date",
        "d32_",
        "Date32",
        "d32_",
    ),
    ChColumn(
        "dtm",
        "DateTime('UTC')",
        "toDateTime('2000-01-01 00:00:00', 'UTC') + n * 3601",
        "timestamp(0)",
        "dtm",
        "DateTime('UTC')",
        "dtm",
    ),
    ChColumn(
        "dt64",
        "DateTime64(6, 'UTC')",
        "toDateTime64('2000-01-01 00:00:00.123456', 6, 'UTC') + n * 3661.000001",
        "timestamp(6)",
        "dt64",
        "DateTime64(6, 'UTC')",
        "dt64",
    ),
    ChColumn(
        "dt9",
        "DateTime64(9, 'UTC')",
        "toDateTime64('2000-01-01 00:00:00.123456789', 9, 'UTC') + n",
        "text",
        "toString(dt9)",
        "String",
        "toDateTime64(dt9, 9, 'UTC')",
    ),
    ChColumn(
        "e8",
        "Enum8('x' = 1, 'y' = 2, 'z' = -3)",
        "CAST(['x', 'y', 'z'][n % 3 + 1], 'Enum8(''x'' = 1, ''y'' = 2, ''z'' = -3)')",
        "text",
        "e8",
        "Enum8('x' = 1, 'y' = 2, 'z' = -3)",
        "e8",
    ),
    ChColumn("u", "UUID", "generateUUIDv4()", "uuid", "u", "UUID", "u"),
    ChColumn(
        "ip4",
        "IPv4",
        "toIPv4(concat('10.', toString(n % 256), '.1.2'))",
        "inet",
        "ip4",
        "IPv4",
        "ip4",
    ),
    ChColumn(
        "ip6",
        "IPv6",
        "toIPv6(concat('2001:db8::', hex(n)))",
        "inet",
        "ip6",
        "IPv6",
        "ip6",
    ),
    ChColumn("b", "Bool", "n % 2 = 0", "boolean", "b", "Bool", "b"),
    ChColumn(
        "arr",
        "Array(Int64)",
        "[toInt64(n), -1, 0]",
        JSON_TYPE,
        "toJSONString(arr)",
        "String",
        "JSONExtract(arr, 'Array(Int64)')",
    ),
    ChColumn(
        "aa",
        "Array(Array(String))",
        "[['a,b', 'c\"d'], [], ['{e}\\t']]",
        JSON_TYPE,
        "toJSONString(aa)",
        "String",
        "JSONExtract(aa, 'Array(Array(String))')",
    ),
    ChColumn(
        "an",
        "Array(Nullable(Int32))",
        "[toNullable(toInt32(n)), null]",
        JSON_TYPE,
        "toJSONString(an)",
        "String",
        "JSONExtract(an, 'Array(Nullable(Int32))')",
    ),
    ChColumn(
        "t",
        "Tuple(a Int32, b String, c Array(Float64))",
        "tuple(toInt32(n), 'x', [1.5, 2.25])",
        JSON_TYPE,
        "toJSONString(t)",
        "String",
        "tuple(JSONExtractInt(t, 'a'), JSONExtractString(t, 'b'), "
        "JSONExtract(t, 'c', 'Array(Float64)'))",
    ),
    ChColumn(
        "m",
        "Map(String, Int64)",
        "map('k', toInt64(n), 'z', 0)",
        JSON_TYPE,
        "toJSONString(m)",
        "String",
        "CAST(JSONExtractKeysAndValues(m, 'Int64'), 'Map(String, Int64)')",
    ),
    ChColumn(
        "mm",
        "Map(String, Array(Int64))",
        "map('k', [toInt64(n)])",
        JSON_TYPE,
        "toJSONString(mm)",
        "String",
        "CAST(JSONExtractKeysAndValues(mm, 'Array(Int64)'), "
        "'Map(String, Array(Int64))')",
    ),
    ChColumn(
        "pt",
        "Point",
        "(toFloat64(n), 0.5)",
        JSON_TYPE,
        "toJSONString(pt)",
        "String",
        "JSONExtract(pt, 'Tuple(Float64, Float64)')",
    ),
    ChColumn(
        "rg",
        "Ring",
        "[(0., 0.), (1., 0.), (toFloat64(n), 1.)]",
        JSON_TYPE,
        "toJSONString(rg)",
        "String",
        "JSONExtract(rg, 'Array(Tuple(Float64, Float64))')",
    ),
    ChColumn(
        "pg",
        "Polygon",
        "[[(0., 0.), (1., 0.), (1., 1.)], "
        "[(0.1, 0.1), (0.2, 0.1), (toFloat64(n), 0.2)]]",
        JSON_TYPE,
        "toJSONString(pg)",
        "String",
        "JSONExtract(pg, 'Array(Array(Tuple(Float64, Float64)))')",
    ),
    ChColumn(
        "ns",
        "Nullable(String)",
        "if(n % 7 = 0, null, toString(n))",
        "text",
        "ns",
        "Nullable(String)",
        "ns",
    ),
    ChColumn(
        "ni",
        "Nullable(Int64)",
        "if(n % 7 = 1, null, toInt64(n))",
        "bigint",
        "ni",
        "Nullable(Int64)",
        "ni",
    ),
    ChColumn(
        "nd",
        "Nullable(Decimal(18, 4))",
        "if(n % 7 = 2, null, toDecimal64(n, 4) / 3)",
        "numeric(18,4)",
        "nd",
        "Nullable(Decimal(18, 4))",
        "nd",
    ),
    ChColumn(
        "ndt",
        "Nullable(DateTime64(3, 'UTC'))",
        "if(n % 7 = 3, null, toDateTime64('2020-01-01', 3, 'UTC') + n)",
        "timestamp(3)",
        "ndt",
        "Nullable(DateTime64(3, 'UTC'))",
        "ndt",
    ),
)
CH_FLOATS = ("f32", "f64")


class Postgres:
    """Сторона postgres: версия, таблицы под матрицу типов, дампы и сверка."""

    SCHEMA: ClassVar[str] = PG_SCHEMA

    def __init__(self, source: PgSource) -> None:
        self.source = source
        self.version = 0

    async def connect(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            cursor = await conn.execute("show server_version_num")
            row = await cursor.fetchone()
            if row is None:
                raise AssertionError("server_version_num returned no row")

            self.version = int(row[0])

    def columns(self) -> list[PgColumn]:
        chosen: list[PgColumn] = []
        for column in PG_COLUMNS:
            if column.min_version <= self.version:
                chosen.append(column)

        return chosen

    async def recreate_typed(self, rows: int) -> None:
        columns = self.columns()
        ddl = ", ".join(f"{c.name} {c.pg_type}" for c in columns)
        selected: list[str] = []
        for column in columns:
            if column.nullable:
                selected.append(
                    f"case when g % 7 = 0 then null else ({column.expression}) end"
                    f" as {column.name}"
                )
                continue

            selected.append(f"({column.expression}) as {column.name}")

        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))
            await conn.execute(self._q(f"create schema {PG_SCHEMA}"))
            await conn.execute(
                self._q(f"create type {PG_SCHEMA}.mood as enum ('sad', 'ok', 'happy')")
            )
            await conn.execute(
                self._q(f"create type {PG_SCHEMA}.pair as (a int, b text)")
            )
            await conn.execute(self._q(f"set search_path to {PG_SCHEMA}"))
            await conn.execute(
                self._q(
                    f"create table {PG_SCHEMA}.src ({ddl}); "
                    f"create table {PG_SCHEMA}.dst ({ddl})"
                )
            )
            await conn.execute(
                self._q(
                    f"insert into {PG_SCHEMA}.src select {', '.join(selected)} "
                    f"from generate_series(1, {rows}) g"
                )
            )

    async def recreate_json_mid(self) -> None:
        json_type = "text"
        if self.version >= 90400:
            json_type = "jsonb"

        parts: list[str] = []
        for column in CH_COLUMNS:
            pg_type = column.pg_type
            if pg_type == JSON_TYPE:
                pg_type = json_type

            parts.append(f"{column.name} {pg_type}")

        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))
            await conn.execute(self._q(f"create schema {PG_SCHEMA}"))
            await conn.execute(
                self._q(f"create table {PG_SCHEMA}.mid ({', '.join(parts)})")
            )

    async def drop(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))

    async def float_mismatches(self, loose: dict[str, str]) -> int:
        """Строки, где float источника и приёмника разошлись больше допуска."""
        checks: list[str] = []
        for name, tolerance in loose.items():
            checks.append(
                f"not (s.{name} is not distinct from d.{name} "
                f"or abs(s.{name} - d.{name}) <= abs(s.{name}) * {tolerance})"
            )

        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            cursor = await conn.execute(
                self._q(
                    f"select count(*) from {PG_SCHEMA}.src s "
                    f"join {PG_SCHEMA}.dst d using (id) where {' or '.join(checks)}"
                )
            )
            row = await cursor.fetchone()

        if row is None:
            raise AssertionError("count returned no row")

        return int(row[0])

    def _q(self, text: str) -> bytes:
        """psycopg принимает литеральную строку или bytes; текст собран."""
        return text.encode()


class ClickHouse:
    """Сторона ClickHouse: версия, настройки, таблицы под матрицу типов."""

    DATABASE: ClassVar[str] = CH_DATABASE

    def __init__(self, source: ChSource) -> None:
        self.source = source
        self.precise_floats = False
        self._settings: dict[str, Any] = {}

    async def connect(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            names = await client.query(
                "select name from system.settings "
                "where name in ('precise_float_parsing', "
                "'allow_experimental_geo_types')"
            )
            for row in names.result_rows:
                if row[0] == "precise_float_parsing":
                    self.precise_floats = True
                    continue

                self._settings[row[0]] = 1

    async def recreate_mid(self, columns: Sequence[PgColumn]) -> None:
        parts: list[str] = []
        for column in columns:
            ch_type = column.ch_type
            if column.nullable:
                ch_type = f"Nullable({ch_type})"

            parts.append(f"{column.name} {ch_type}")

        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await self._fresh_database(client)
            await client.command(
                f"create table {self.DATABASE}.mid ({', '.join(parts)}) "
                "engine = MergeTree order by id",
                settings=self._settings,
            )

    async def recreate_typed(self, rows: int) -> None:
        ddl = ", ".join(f"{c.name} {c.ch_type}" for c in CH_COLUMNS)
        selected = ", ".join(f"({c.expression}) as {c.name}" for c in CH_COLUMNS)
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await self._fresh_database(client)
            for table in ("src", "dst"):
                await client.command(
                    f"create table {self.DATABASE}.{table} ({ddl}) "
                    "engine = MergeTree order by id",
                    settings=self._settings,
                )

            await client.command(
                f"insert into {CH_DATABASE}.src select {selected} "
                f"from (select number as n from numbers(1, {rows}))",
                settings=self._settings,
            )

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {self.DATABASE}")

    async def float_mismatches(self, loose: dict[str, str]) -> int:
        checks: list[str] = []
        for name, tolerance in loose.items():
            checks.append(
                f"not (s.{name} = d.{name} or (isNaN(s.{name}) and isNaN(d.{name})) "
                f"or abs(s.{name} - d.{name}) <= abs(s.{name}) * {tolerance})"
            )

        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query(
                f"select count() from {CH_DATABASE}.src s "
                f"join {CH_DATABASE}.dst d using id where {' or '.join(checks)}"
            )

        return int(result.result_rows[0][0])

    async def _fresh_database(self, client: Any) -> None:
        await client.command(f"drop database if exists {self.DATABASE}")
        await client.command(f"create database {self.DATABASE}")


def _names(
    columns: Sequence[Any], skipped: Sequence[str] | dict[str, str] = ()
) -> list[str]:
    names: list[str] = []
    for column in columns:
        if column.name in skipped:
            continue

        names.append(column.name)

    return names


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[Postgres]:
    side = Postgres(request.param)
    await side.connect()
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouse]:
    side = ClickHouse(request.param)
    await side.connect()
    yield side
    await side.drop()


class TestPostgresToClickHouseAndBack:
    async def test_every_postgres_type_survives(
        self, postgres: Postgres, clickhouse: ClickHouse
    ) -> None:
        columns = postgres.columns()
        names = _names(columns)
        await postgres.recreate_typed(ROWS)
        await clickhouse.recreate_mid(columns)
        pumps = Pumps(postgres.source.postgres, clickhouse.source.admin)
        listed = ", ".join(names)

        exported = await pumps.pg_out(
            f"copy (select {listed} from {PG_SCHEMA}.src order by id) to stdout"
        )
        if clickhouse.precise_floats:
            structure: list[str] = []
            selected: list[str] = []
            for column in columns:
                if column.name in PG_FLOATS:
                    text_type = "String"
                    if column.nullable:
                        text_type = "Nullable(String)"

                    structure.append(f"{column.name} {text_type}")
                    selected.append(f"to{column.ch_type}({column.name})")
                    continue

                ch_type = column.ch_type
                if column.nullable:
                    ch_type = f"Nullable({ch_type})"

                structure.append(f"{column.name} {ch_type}")
                selected.append(column.name)

            insert = (
                f"insert into {CH_DATABASE}.mid ({listed}) "
                f"select {', '.join(selected)} from input('{', '.join(structure)}') "
                "settings precise_float_parsing = 1 format TabSeparated"
            )
        else:
            insert = f"insert into {CH_DATABASE}.mid ({listed}) format TabSeparated"

        report = await pumps.ch_in(insert, exported)
        assert f"{ROWS} rows written" in report

        landed = await pumps.ch_out(
            f"select {listed} from {CH_DATABASE}.mid order by id format TabSeparated"
        )
        report = await pumps.pg_in(
            f"copy {PG_SCHEMA}.dst ({listed}) from stdin", landed
        )
        assert f"COPY {ROWS}" in report

        tolerance = FloatTolerance(postgres.version, clickhouse.precise_floats)
        loose = tolerance.loose(PG_FLOATS)
        if loose:
            assert await postgres.float_mismatches(loose) == 0

        exact = ", ".join(_names(columns, loose))
        before = await pumps.pg_out(
            f"copy (select {exact} from {PG_SCHEMA}.src order by id) to stdout"
        )
        after = await pumps.pg_out(
            f"copy (select {exact} from {PG_SCHEMA}.dst order by id) to stdout"
        )
        assert before.count(b"\n") == ROWS
        assert after == before


class TestClickHouseToPostgresAndBack:
    async def test_every_clickhouse_type_survives(
        self, postgres: Postgres, clickhouse: ClickHouse
    ) -> None:
        names = _names(CH_COLUMNS)
        await clickhouse.recreate_typed(ROWS)
        await postgres.recreate_json_mid()
        pumps = Pumps(postgres.source.postgres, clickhouse.source.admin)
        listed = ", ".join(names)
        outbound = ", ".join(f"{c.outbound} as {c.name}" for c in CH_COLUMNS)

        exported = await pumps.ch_out(
            f"select {outbound} from {CH_DATABASE}.src order by id format TabSeparated"
        )
        report = await pumps.pg_in(
            f"copy {PG_SCHEMA}.mid ({listed}) from stdin", exported
        )
        assert f"COPY {ROWS}" in report

        landed = await pumps.pg_out(
            f"copy (select {listed} from {PG_SCHEMA}.mid order by id) to stdout"
        )
        structure = ", ".join(f"{c.name} {c.input_type}" for c in CH_COLUMNS)
        inbound = ", ".join(f"{c.inbound} as {c.name}" for c in CH_COLUMNS)
        precise = ""
        if clickhouse.precise_floats:
            precise = " settings precise_float_parsing = 1"

        quoted = structure.replace("'", "''")
        report = await pumps.ch_in(
            f"insert into {CH_DATABASE}.dst select {inbound} from input('{quoted}')"
            f"{precise} format TabSeparated",
            landed,
        )
        assert f"{ROWS} rows written" in report

        tolerance = FloatTolerance(postgres.version, clickhouse.precise_floats)
        loose = tolerance.loose(CH_FLOATS)
        if loose:
            assert await clickhouse.float_mismatches(loose) == 0

        exact = ", ".join(_names(CH_COLUMNS, loose))
        before = await pumps.ch_out(
            f"select {exact} from {CH_DATABASE}.src order by id format TabSeparated"
        )
        after = await pumps.ch_out(
            f"select {exact} from {CH_DATABASE}.dst order by id format TabSeparated"
        )
        assert before.count(b"\n") == ROWS
        assert after == before


@pytest.fixture(scope="module")
async def newest_postgres() -> AsyncIterator[Postgres]:
    side = Postgres(STAND.sources[-1])
    await side.connect()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def newest_clickhouse() -> AsyncIterator[ClickHouse]:
    side = ClickHouse(STAND.demo_clickhouse()[-1])
    await side.connect()
    yield side
    await side.drop()


class TestVolume:
    """Много строк на новейшей паре: 300 000 записей со всеми типами postgres
    едут туда и обратно, счётчики и дампы сходятся."""

    async def test_large_stream_survives_both_ways(
        self, newest_postgres: Postgres, newest_clickhouse: ClickHouse
    ) -> None:
        postgres = newest_postgres
        clickhouse = newest_clickhouse
        columns = postgres.columns()
        names = _names(columns)
        await postgres.recreate_typed(BIG_ROWS)
        await clickhouse.recreate_mid(columns)
        pumps = Pumps(postgres.source.postgres, clickhouse.source.admin)
        listed = ", ".join(names)

        exported = await pumps.pg_out(
            f"copy (select {listed} from {PG_SCHEMA}.src order by id) to stdout"
        )
        report = await pumps.ch_in(
            f"insert into {CH_DATABASE}.mid ({listed}) format TabSeparated",
            exported,
            BIG_CHUNK,
        )
        assert f"{BIG_ROWS} rows written" in report

        landed = await pumps.ch_out(
            f"select {listed} from {CH_DATABASE}.mid order by id format TabSeparated"
        )
        report = await pumps.pg_in(
            f"copy {PG_SCHEMA}.dst ({listed}) from stdin",
            landed,
            BIG_CHUNK,
        )
        assert f"COPY {BIG_ROWS}" in report

        exact = ", ".join(_names(columns, PG_FLOATS))
        before = await pumps.pg_out(
            f"copy (select {exact} from {PG_SCHEMA}.src order by id) to stdout"
        )
        after = await pumps.pg_out(
            f"copy (select {exact} from {PG_SCHEMA}.dst order by id) to stdout"
        )
        assert before.count(b"\n") == BIG_ROWS
        assert after == before
