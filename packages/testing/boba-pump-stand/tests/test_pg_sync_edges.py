"""Пограничные случаи приёмника pg_sync_in: NULL и not null, decimal с
точностью и NaN, varchar с юникодом и длиной, timestamp с точностью, поясом
и бесконечностью, геометрия, диапазоны, json, массивы, пользовательские
типы; кросс-движковые потоки из ClickHouse (беззнаковые целые, наносекунды)
и Oracle (NUMBER без точности, NaN у BINARY_DOUBLE, CLOB в мегабайты).
Постгресы — по одному на поколение плюс Greenplum 6 и 7."""

# ruff: noqa: S608, PLR0913

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from boba.db.clickhouse.target import ChStreamWire
from boba.db.postgres import PgArrowError
from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.transfer import PgColumnDeclaration
from boba.pump_stand import (
    ClickHouseSide,
    Leg,
    Loaded,
    OracleSide,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.pump_stand.stand import PgSource
from boba.stand.ix import IxStand
from boba.toolkit.transfer import (
    ColumnRules,
    CreateIfNotExists,
    DeleteNothing,
    DoNothing,
    DropAndCreateIfSchemaChanged,
    ErrorIfSchemaChanged,
    FailOnUnknown,
    FallbackAsVarchar,
    InsertFull,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
CHUNK = 4096
S = "pump_edges"
ROWS = 7
NULL_ROW = 5
TARGETS = ("pg-9.0", "pg-12", "pg-19", "gp-6", "gp-7")
STRING_AS_STRING = "output_format_arrow_string_as_string = 1"


def _chosen(sources: Sequence[Any]) -> list[Any]:
    chosen: list[Any] = []
    for source in sources:
        if source.name in TARGETS:
            chosen.append(source)

    return chosen


def _newest(sources: Sequence[Any]) -> str:
    plain: list[str] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source.name)

    return plain[-1]


NEWEST = _newest(STAND.sources)


@pytest.fixture(scope="module", params=_chosen(STAND.sources), ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, S)
    await side.connect()
    await side.recreate_schema(
        [
            "create type mood as enum ('sad', 'happy')",
            "create type pair as (a int, b text)",
            "create domain posint as int check (value > 0)",
        ]
    )
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(STAND.demo_clickhouse()[-1], S)
    await side.connect()
    await side.recreate_database()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def oracle() -> AsyncIterator[OracleSide]:
    side = OracleSide(max(STAND.ora_sources, key=lambda s: s.name), 500)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


def only_newest(postgres: PostgresSide) -> None:
    if postgres.source.name != NEWEST:
        pytest.skip("one postgres is enough here")


async def load(
    postgres: PostgresSide,
    source: str,
    target: str,
    schema: Any = CreateIfNotExists(kind="create_if_not_exists"),
    rules: ColumnRules = ColumnRules(),
    where: str = "",
    chunk: int = CHUNK,
    mode: StreamWire = StreamWire.ARROW,
    columns: Sequence[PgColumnDeclaration] = (),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
) -> str:
    """pg_sync_out таблицы источника -> pg_sync_in приёмника в той же схеме;
    mode — движок приёмника для выбора формата (unknown — Arrow)."""
    select = f"select * from {S}.{source}"
    if where:
        select = f"{select} where {where}"

    declared = [PgColumnDeclaration(name="id", nullable=False), *columns]

    return await load_select(
        postgres, select, target, schema, rules, chunk, mode, declared, unknown_types
    )


async def load_select(
    postgres: PostgresSide,
    select: str,
    target: str,
    schema: Any,
    rules: ColumnRules = ColumnRules(),
    chunk: int = CHUNK,
    mode: StreamWire = StreamWire.ARROW,
    columns: Sequence[PgColumnDeclaration] = (),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
) -> str:
    """pg_sync_out произвольного select -> pg_sync_in."""
    pumps = Pumps(postgres=postgres.profile)
    chained = await pumps.chain(
        Leg(
            "pg_sync_out",
            {
                "sql": select,
                "wire": mode,
                "columns": columns,
                "copy_options": CopyOptions(chunk_bytes=chunk),
            },
        ),
        Leg(
            "pg_sync_in",
            {
                "schema_name": S,
                "table_name": target,
                "schema_strategy": schema,
                "delete_strategy": DeleteNothing(kind="nothing"),
                "insert_strategy": InsertFull(kind="full"),
                "rules": rules,
                "unknown_types": unknown_types,
                "copy_options": CopyOptions(
                    chunk_bytes=chunk, exact_floats=postgres.greenplum_6
                ),
            },
        ),
    )

    return chained.in_report


async def fill(postgres: PostgresSide, table: str, kind: str, expr: str) -> None:
    """Таблица id + одна колонка kind, ROWS строк по выражению над g, строка
    NULL_ROW — NULL."""
    await postgres.execute(
        [
            f"drop table if exists {S}.{table}",
            f"create table {S}.{table} (id bigint not null, v {kind})",
            f"insert into {S}.{table} select g, case when g = {NULL_ROW} "
            f"then null else ({expr}) end from generate_series(1, {ROWS}) g",
        ]
    )


@dataclass(frozen=True)
class Case:
    """Тип источника: DDL, заполнение над g, минимальная версия сервера и
    тип, который ждём у приёмника (пусто — тот же, что у источника)."""

    name: str
    kind: str
    expr: str
    min_version: int = 0
    lands_as: str = ""


CASES = [
    Case("ts_bc", "timestamp", "timestamp '0001-01-01 00:00:00'"),
    Case(
        "ts_max",
        "timestamp(6)",
        "timestamp '9999-12-31 23:59:59.999999' - g * interval '1 us'",
    ),
    Case(
        "ts_ms",
        "timestamp(3)",
        "timestamp '2024-02-29 13:14:15.123' + g * interval '1 ms'",
    ),
    Case(
        "tstz",
        "timestamptz",
        "timestamptz '2024-03-31 02:30:00 Europe/Moscow' + g * interval '1 hour'",
    ),
    Case("t6", "time(6)", "time '23:59:59.999999' - g * interval '1 us'"),
    Case("ttz", "timetz", "timetz '12:00:00+05:30'"),
    Case("iv", "interval", "interval '1 year 2 mons 3 days 04:05:06.789' * g"),
    Case("d", "date", "date '0001-01-01' + g"),
    Case("pt", "point", "point(g * 1.5, -2.25)"),
    Case("bx", "box", "box(point(0,0), point(g,1))"),
    Case("pl", "polygon", "polygon '((0,0),(1,0),(1,1))'"),
    Case("ci", "circle", "circle(point(0,0), g)"),
    Case("ln", "line", "line '{1,2,3}'", 90400),
    Case(
        "js",
        "jsonb",
        "jsonb_build_object('a', g, 'b', array[1,2], 'c', null, 'd', 'x\"y\\n')",
        90500,
    ),
    Case("jsn", "json", '\'{"k": "v"}\'::json', 90200),
    Case("u", "uuid", "'a1b2c3d4-0000-0000-0000-000000000001'::uuid"),
    Case("ip", "inet", "'10.0.0.1/24'::inet"),
    Case("cidr", "cidr", "'10.0.0.0/8'::cidr"),
    Case("mac", "macaddr", "'08:00:2b:01:02:03'::macaddr"),
    Case("by", "bytea", "decode('00ff10' || lpad(g::text, 2, '0'), 'hex')"),
    Case("arr", "int[]", "array[g, null, 3]"),
    Case(
        "arr2",
        "text[]",
        "array['a b', 'q\"uote', 'back\\slash', 'comma,', '', 'NULL', null]",
    ),
    Case("arr2d", "int[][]", "array[array[1,2],array[3,4]]", 0, "integer[]"),
    Case("narr", "numeric(5,2)[]", "array[1.25, 2.5]"),
    Case("en", "mood", "'happy'::mood", 0, "character varying"),
    Case("comp", "pair", "row(g, 'x')::pair", 0, "character varying"),
    Case("rng", "int4range", "int4range(1, g + 1)", 90200),
    Case("tsr", "tstzrange", "tstzrange('2024-01-01', '2024-02-01')", 90200),
    Case("mon", "money", "'12.5'::money * g"),
    Case("bt", "bit(8)", "B'10101010'"),
    Case("vb", "varbit", "B'101'"),
    Case("xm", "xml", "'<a b=\"1\">x</a>'::xml"),
    Case("tsv", "tsvector", "to_tsvector('english', 'the quick brown fox')"),
    Case("ch5", "char(5)", "'ab'"),
    Case("vcu", "varchar(5)", "'😀😀😀😀😀'"),
    Case(
        "txt",
        "text",
        "case g when 1 then '' when 2 then E'\\\\N' when 3 then E'a\\tb\\nc' "
        "else E'q''\"\\\\' end",
    ),
    Case("dom", "posint", "g", 0, "integer"),
    Case(
        "f8",
        "float8",
        "case g when 1 then 'NaN'::float8 when 2 then 'inf' when 3 then '-inf' "
        "else g / 3.0 end",
    ),
    Case("f4", "real", "(g / 3.0)::real"),
    Case("big", "numeric(38,10)", "9999999999999999999999999999.9999999999 - g"),
    Case("neg", "numeric(18,4)", "-g / 7.0"),
    Case("i2", "smallint", "-32768 + g"),
    Case("b", "boolean", "g % 2 = 0"),
]


class TestRoundTrip:
    """Каждый тип: таблица-источник с NULL в одной строке, pg_sync_out ->
    pg_sync_in создаёт приёмник, значения текстом равны, тип приёмника
    ожидаемый."""

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
    async def test_type_lands_as_itself(
        self, postgres: PostgresSide, case: Case
    ) -> None:
        if postgres.version < case.min_version:
            pytest.skip(f"{case.kind} needs server {case.min_version}")

        try:
            await fill(postgres, f"s_{case.name}", case.kind, case.expr)
        except psycopg.errors.FeatureNotSupported as exc:
            pytest.skip(f"{case.kind} on this server: {exc}")

        report = await load(
            postgres,
            f"s_{case.name}",
            f"t_{case.name}",
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        source = Loaded(postgres, S, f"s_{case.name}")
        target = Loaded(postgres, S, f"t_{case.name}")
        source_type = (await source.columns())[1][1]
        expected = case.lands_as
        if not expected:
            expected = source_type

        assert report.startswith(f"{ROWS} rows written")
        assert (await target.columns())[1] == ("v", expected, False)
        assert await target.texts("v") == await source.texts("v")


class TestRefusedValues:
    """Значения, которых читатель CSV Arrow не собирает: отказ с причиной."""

    async def test_numeric_nan_needs_a_cast(self, postgres: PostgresSide) -> None:
        only_newest(postgres)
        await fill(
            postgres,
            "s_nan",
            "numeric(10,2)",
            "case g when 1 then 'NaN'::numeric else g end",
        )
        with pytest.raises(PgArrowError, match="NaN"):
            await load(postgres, "s_nan", "t_nan")

        report = await load_select(
            postgres,
            f"select id, v::float8 as v from {S}.s_nan",
            "t_nan_f8",
            CreateIfNotExists(kind="create_if_not_exists"),
        )

        assert report.startswith(f"{ROWS} rows written")
        assert (await Loaded(postgres, S, "t_nan_f8").texts("v"))[0] == "NaN"

    async def test_timestamp_infinity_needs_a_cast(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        await fill(
            postgres,
            "s_inf",
            "timestamp",
            "case g when 1 then 'infinity'::timestamp when 2 then '-infinity' "
            "else timestamp '2024-01-01' end",
        )
        with pytest.raises(PgArrowError, match="infinity"):
            await load(postgres, "s_inf", "t_inf")

        report = await load_select(
            postgres,
            f"select id, v::text as v from {S}.s_inf",
            "t_inf_text",
            CreateIfNotExists(kind="create_if_not_exists"),
        )

        assert report.startswith(f"{ROWS} rows written")
        assert (await Loaded(postgres, S, "t_inf_text").texts("v"))[:2] == [
            "infinity",
            "-infinity",
        ]

    async def test_numeric_without_precision_is_refused_before_execution(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        await fill(postgres, "s_free", "numeric", "g / 7.0")
        with pytest.raises(PgArrowError, match="numeric without precision"):
            await load(postgres, "s_free", "t_free")

    async def test_numeric_wider_than_decimal128_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        await fill(postgres, "s_wide", "numeric(40,2)", "g")
        with pytest.raises(PgArrowError, match="up to 38 digits"):
            await load(postgres, "s_wide", "t_wide")


class TestNulls:
    async def test_nulls_and_empty_strings_are_distinct(
        self, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_nul",
            "text",
            "case g when 1 then '' when 2 then E'\\\\N' else 'x' end",
        )
        await load(postgres, "s_nul", "t_nul")
        values = await Loaded(postgres, S, "t_nul").texts("v")

        assert values[0] == ""
        assert values[1] == "\\N"
        assert values[NULL_ROW - 1] is None
        assert values.count(None) == 1

    async def test_not_null_travels_and_a_nullable_stream_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.s_strict",
                f"create table {S}.s_strict (id bigint not null, v int not null)",
                f"insert into {S}.s_strict select g, g "
                f"from generate_series(1, {ROWS}) g",
            ]
        )
        await load(
            postgres,
            "s_strict",
            "t_strict",
            columns=[PgColumnDeclaration(name="v", nullable=False)],
        )

        assert (await Loaded(postgres, S, "t_strict").columns())[1] == (
            "v",
            "integer",
            True,
        )

        with pytest.raises(
            TransferError, match="stream is nullable, table column is not null"
        ):
            await load_select(
                postgres,
                f"select id, v from {S}.s_strict",
                "t_strict",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

    async def test_null_into_not_null_rolls_the_whole_load_back(
        self, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.t_rollback",
                f"create table {S}.t_rollback (id bigint not null, v int not null)",
                f"insert into {S}.t_rollback values (0, 0)",
            ]
        )
        with pytest.raises(psycopg.errors.NotNullViolation):
            await load_select(
                postgres,
                f"select g::bigint as id, "
                f"case when g = {NULL_ROW} then null else g end as v "
                f"from generate_series(1, {ROWS}) g",
                "t_rollback",
                DoNothing(kind="do_nothing"),
            )

        assert await Loaded(postgres, S, "t_rollback").count() == 1


class TestDecimal:
    async def test_values_are_exact(self, postgres: PostgresSide) -> None:
        await fill(
            postgres,
            "s_dec",
            "numeric(18,4)",
            "case g when 1 then 0 when 2 then -0.0001 when 3 then 99999999999999.9999 "
            "when 4 then -99999999999999.9999 else g / 7.0 end",
        )
        await load(postgres, "s_dec", "t_dec")
        landed = await Loaded(postgres, S, "t_dec").scalars("v")

        assert landed[:4] == [
            Decimal("0.0000"),
            Decimal("-0.0001"),
            Decimal("99999999999999.9999"),
            Decimal("-99999999999999.9999"),
        ]
        assert landed[NULL_ROW - 1] is None

    async def test_declaration_widens_the_target_and_keeps_values(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_dec_w", "numeric(18,4)", "g / 7.0")
        await load(
            postgres,
            "s_dec_w",
            "t_dec_w",
            rules=ColumnRules(column_types={"v": "numeric(20,6)"}),
        )
        loaded = Loaded(postgres, S, "t_dec_w")

        assert (await loaded.columns())[1] == ("v", "numeric(20,6)", False)
        assert (await loaded.scalars("v"))[0] == Decimal("0.142900")

    @pytest.mark.parametrize(
        ("target_type", "reason"),
        [
            ("numeric(10,2)", "truncates the scale"),
            ("numeric(18,2)", "truncates the scale"),
            ("numeric(15,4)", "fewer integer digits"),
            ("bigint", "type family differs"),
            ("double precision", "type family differs"),
        ],
    )
    async def test_narrower_target_is_a_schema_change(
        self, postgres: PostgresSide, target_type: str, reason: str
    ) -> None:
        await fill(postgres, "s_dec_n", "numeric(18,4)", "g / 7.0")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_dec_n",
                f"create table {S}.t_dec_n (id bigint not null, v {target_type})",
            ]
        )
        with pytest.raises(TransferError, match=reason):
            await load(
                postgres,
                "s_dec_n",
                "t_dec_n",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

    async def test_wider_target_is_only_a_warning(self, postgres: PostgresSide) -> None:
        await fill(postgres, "s_dec_ok", "numeric(18,4)", "g / 7.0")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_dec_ok",
                f"create table {S}.t_dec_ok (id bigint not null, v numeric(20,6))",
            ]
        )
        report = await load(
            postgres,
            "s_dec_ok",
            "t_dec_ok",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
        )

        assert "- warning v: table numeric128" not in report
        assert "- warning v:" in report
        assert await Loaded(postgres, S, "t_dec_ok").count() == ROWS


class TestVarchar:
    async def test_unicode_length_counts_characters(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_uni", "varchar(7)", "repeat('😀', g)")
        await load(postgres, "s_uni", "t_uni")
        loaded = Loaded(postgres, S, "t_uni")

        assert (await loaded.columns())[1] == ("v", "character varying(7)", False)
        assert (await loaded.texts("v"))[6] == "😀" * 7

    async def test_longer_stream_into_shorter_column_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_v100", "varchar(100)", "repeat('x', g)")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_v50",
                f"create table {S}.t_v50 (id bigint not null, v varchar(50))",
            ]
        )
        with pytest.raises(
            TransferError, match="table length 50 is shorter than stream length 100"
        ):
            await load(
                postgres,
                "s_v100",
                "t_v50",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

        report = await load(
            postgres,
            "s_v100",
            "t_v50",
            DropAndCreateIfSchemaChanged(kind="drop_and_create_if_schema_changed"),
        )

        assert "schema: drop_then_create" in report
        assert (await Loaded(postgres, S, "t_v50").columns())[1] == (
            "v",
            "character varying(100)",
            False,
        )

    async def test_text_into_limited_column_warns_and_the_server_checks_values(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_text", "text", "repeat('y', g)")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_lim",
                f"create table {S}.t_lim (id bigint not null, v varchar(10))",
            ]
        )
        report = await load(
            postgres,
            "s_text",
            "t_lim",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
        )

        assert "warning v: table limits the length to 10" in report

        await fill(postgres, "s_text_long", "text", "repeat('y', g * 10)")
        with pytest.raises(psycopg.errors.StringDataRightTruncation):
            await load(postgres, "s_text_long", "t_lim", DoNothing(kind="do_nothing"))

        assert await Loaded(postgres, S, "t_lim").count() == ROWS

    async def test_char_padding_survives(self, postgres: PostgresSide) -> None:
        await fill(postgres, "s_ch", "char(5)", "'a' || g")
        await load(postgres, "s_ch", "t_ch")

        assert (await Loaded(postgres, S, "t_ch").scalars("length(v)"))[0] == 2
        assert (await Loaded(postgres, S, "t_ch").scalars("octet_length(v)"))[0] == 5


class TestTimestamps:
    async def test_milliseconds_keep_their_precision(
        self, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_ms",
            "timestamp(3)",
            "timestamp '2024-02-29 13:14:15.123' + g * interval '1 ms'",
        )
        await load(postgres, "s_ms", "t_ms")
        loaded = Loaded(postgres, S, "t_ms")

        assert (await loaded.columns())[1] == (
            "v",
            "timestamp(3) without time zone",
            False,
        )
        assert (await loaded.texts("v"))[0] == "2024-02-29 13:14:15.124"

    async def test_finer_stream_into_coarser_column_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres, "s_us", "timestamp(6)", "timestamp '2024-02-29 13:14:15.123456'"
        )
        await postgres.execute(
            [
                f"drop table if exists {S}.t_s0",
                f"create table {S}.t_s0 (id bigint not null, v timestamp(0))",
            ]
        )
        with pytest.raises(TransferError, match="coarser"):
            await load(
                postgres,
                "s_us",
                "t_s0",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

    async def test_coarser_stream_into_finer_column_only_warns(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_s0", "timestamp(0)", "timestamp '2024-02-29 13:14:15'")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_us",
                f"create table {S}.t_us (id bigint not null, v timestamp(6))",
            ]
        )
        report = await load(
            postgres,
            "s_s0",
            "t_us",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
        )

        assert "warning v:" in report
        assert "finer" in report

    async def test_time_zone_mismatch_is_refused(self, postgres: PostgresSide) -> None:
        await fill(
            postgres, "s_tz", "timestamptz", "timestamptz '2024-02-29 13:14:15+03'"
        )
        await postgres.execute(
            [
                f"drop table if exists {S}.t_naive",
                f"create table {S}.t_naive (id bigint not null, v timestamp)",
            ]
        )
        with pytest.raises(TransferError, match="time zone differs"):
            await load(
                postgres,
                "s_tz",
                "t_naive",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

    async def test_instant_survives_dst_and_session_zones(
        self, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_dst",
            "timestamptz",
            "timestamptz '2024-03-31 02:30:00 Europe/Moscow' + g * interval '1 hour'",
        )
        await load(postgres, "s_dst", "t_dst")
        source = Loaded(postgres, S, "s_dst")
        target = Loaded(postgres, S, "t_dst")

        assert await target.scalars("extract(epoch from v)") == await source.scalars(
            "extract(epoch from v)"
        )

    async def test_date_into_timestamp_is_a_family_change(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_date", "date", "date '2024-02-29' + g")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_ts_for_date",
                f"create table {S}.t_ts_for_date (id bigint not null, v timestamp)",
            ]
        )
        with pytest.raises(TransferError, match="type family differs"):
            await load(
                postgres,
                "s_date",
                "t_ts_for_date",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )


class TestFloats:
    async def test_doubles_land_bit_for_bit(self, postgres: PostgresSide) -> None:
        """Включая 1.942e-297, которое Greenplum 6 в десятичной записи
        округляет: у него загрузка идёт hex-записью."""
        await fill(
            postgres,
            "s_bits",
            "float8",
            "case g when 1 then 1942::float8 * 1e-300 when 2 then "
            "2.2250738585072014e-308 "
            "when 3 then 1e308 when 4 then -0.0 else g / 3.0 end",
        )
        await load(postgres, "s_bits", "t_bits")
        source = Loaded(postgres, S, "s_bits")
        target = Loaded(postgres, S, "t_bits")

        assert await target.scalars("float8send(v)::text") == await source.scalars(
            "float8send(v)::text"
        )


class TestClickHouseSources:
    async def test_unsigned_and_nanoseconds_land(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        only_newest(postgres)
        await clickhouse.create(
            "edges",
            [
                "id Int64",
                "u64 UInt64",
                "u8 UInt8",
                "d DateTime64(9, 'UTC')",
                "s Nullable(String)",
                "dec Decimal(18, 4)",
            ],
        )
        await clickhouse.command(
            f"insert into {S}.edges select number, 18446744073709551615 - number, "
            "255 - toUInt8(number), "
            "toDateTime64('2024-02-29 13:14:15.123456789', 9, 'UTC'), "
            "if(number = 2, null, concat('s', toString(number))), "
            "toDecimal64(number / 7, 4) "
            "from numbers(5)"
        )
        pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.profile)
        chained = await pumps.chain(
            Leg(
                "ch_sync_out",
                {
                    "sql": f"select * from {S}.edges order by id "
                    f"settings {STRING_AS_STRING}",
                    "wire": ChStreamWire.ARROW,
                    "columns": [],
                    "chunk_bytes": CHUNK,
                },
            ),
            Leg(
                "pg_sync_in",
                {
                    "schema_name": S,
                    "table_name": "from_ch",
                    "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK),
                },
            ),
        )
        loaded = Loaded(postgres, S, "from_ch")
        columns = await loaded.columns()

        assert chained.in_report.startswith("5 rows written")
        assert ("u64", "numeric(20,0)", True) in columns
        assert ("u8", "smallint", True) in columns
        assert ("d", "timestamp(6) with time zone", True) in columns
        assert ("s", "text", False) in columns
        assert ("dec", "numeric(18,4)", True) in columns
        assert (await loaded.scalars("u64"))[0] == Decimal("18446744073709551615")
        assert (await loaded.texts("d"))[0] == "2024-02-29 13:14:15.123457+00"
        assert (await loaded.texts("s"))[2] is None

    async def test_unsigned_into_signed_column_is_refused(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        only_newest(postgres)
        await clickhouse.create("u64only", ["id Int64", "u64 UInt64"])
        await postgres.execute(
            [
                f"drop table if exists {S}.t_u64",
                f"create table {S}.t_u64 (id bigint, u64 bigint)",
            ]
        )
        pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.profile)
        with pytest.raises(TransferError, match="narrower than stream uint64"):
            await pumps.chain(
                Leg(
                    "ch_sync_out",
                    {
                        "sql": f"select * from {S}.u64only",
                        "wire": ChStreamWire.ARROW,
                        "columns": [],
                        "chunk_bytes": CHUNK,
                    },
                ),
                Leg(
                    "pg_sync_in",
                    {
                        "schema_name": S,
                        "table_name": "t_u64",
                        "schema_strategy": ErrorIfSchemaChanged(
                            kind="error_if_schema_changed"
                        ),
                        "delete_strategy": DeleteNothing(kind="nothing"),
                        "insert_strategy": InsertFull(kind="full"),
                        "copy_options": CopyOptions(chunk_bytes=CHUNK),
                    },
                ),
            )


class TestOracleSources:
    async def test_number_without_precision_nan_doubles_and_a_big_clob(
        self, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        only_newest(postgres)
        await oracle.create(
            "EDGES",
            [
                "ID number(10) not null",
                "N number",
                "BD binary_double",
                "TXT clob",
                "VC nvarchar2(20)",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser.NAME}.edges values "
                "(1, 12345678901234567890123456789012345678, binary_double_nan, "
                "to_clob(rpad('c', 4000, 'c')) || to_clob(rpad('d', 4000, 'd')), "
                "'кириллица 中文')",
                f"insert into {PumpUser.NAME}.edges values "
                "(2, -1, binary_double_infinity, null, null)",
                f"insert into {PumpUser.NAME}.edges values "
                "(3, null, 1.5, empty_clob(), '')",
            )
        )
        pumps = Pumps(postgres=postgres.profile, oracle=oracle.profile)
        chained = await pumps.chain(
            Leg(
                "ora_sync_out",
                {
                    "sql": f"select * from {PumpUser.NAME}.edges",
                    "columns": [PgColumnDeclaration(name="ID", nullable=False)],
                },
            ),
            Leg(
                "pg_sync_in",
                {
                    "schema_name": S,
                    "table_name": "from_ora",
                    "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK),
                },
            ),
        )
        loaded = Loaded(postgres, S, "from_ora")
        columns = await loaded.columns()

        assert chained.in_report.startswith("3 rows written")
        assert ("ID", "numeric(10,0)", True) in columns
        assert ("N", "numeric(38,0)", False) in columns
        assert ("BD", "double precision", False) in columns
        assert ("TXT", "text", False) in columns
        assert (await loaded.scalars('"N"', "ID"))[0] == Decimal(
            "12345678901234567890123456789012345678"
        )
        assert await loaded.texts("BD", "ID") == ["NaN", "Infinity", "1.5"]
        assert (await loaded.scalars('length("TXT")', "ID")) == [8000, None, None]
        assert (await loaded.texts("VC", "ID"))[0] == "кириллица 中文"


COPY_CASES = [
    Case(
        "ts_infinity",
        "timestamp",
        "case g when 1 then 'infinity'::timestamp when 2 then '-infinity' "
        "else timestamp '2024-01-01' end",
    ),
    Case(
        "date_infinity",
        "date",
        "case g when 1 then 'infinity'::date else date '2024-02-29' end",
    ),
    Case(
        "numeric_nan", "numeric(10,2)", "case g when 1 then 'NaN'::numeric else g end"
    ),
    Case("numeric_999", "numeric(999,5)", "12345678901234567890.12345 * g"),
    Case("numeric_free", "numeric", "g / 7.0"),
    Case("range", "int4range", "int4range(1, g + 1)", 90200),
]
"""Что Arrow не несёт, а COPY переносит как есть."""

COPY_EXPECTED = {
    "ts_infinity": ["infinity", "-infinity", "2024-01-01 00:00:00"],
    "date_infinity": ["infinity", "2024-02-29", "2024-02-29"],
    "numeric_nan": ["NaN", "2.00", "3.00"],
    "numeric_999": [
        "12345678901234567890.12345",
        "24691357802469135780.24690",
        "37037036703703703670.37035",
    ],
    "numeric_free": [
        "0.14285714285714285714",
        "0.28571428571428571429",
        "0.42857142857142857143",
    ],
    "range": ["[1,2)", "[1,3)", "[1,4)"],
}


class TestCopyMode:
    """pg -> postgres по COPY csv: то, чего Arrow не несёт, едет как есть."""

    @pytest.mark.parametrize("case", COPY_CASES, ids=lambda c: c.name)
    async def test_values_arrow_cannot_carry_travel_verbatim(
        self, postgres: PostgresSide, case: Case
    ) -> None:
        if postgres.version < case.min_version:
            pytest.skip(f"{case.kind} needs server {case.min_version}")

        await fill(postgres, f"s_{case.name}", case.kind, case.expr)
        report = await load(
            postgres, f"s_{case.name}", f"t_{case.name}", mode=StreamWire.CSV
        )
        target = Loaded(postgres, S, f"t_{case.name}")
        source = Loaded(postgres, S, f"s_{case.name}")

        assert report.startswith(f"{ROWS} rows written")
        assert (await target.texts("v"))[:3] == COPY_EXPECTED[case.name]
        assert await target.texts("v") == await source.texts("v")
        assert (await target.columns())[1][1] == (await source.columns())[1][1]

    async def test_enum_needs_a_declared_type_to_stay_an_enum(
        self, postgres: PostgresSide
    ) -> None:
        """Имени enum протокол не несёт (только OID): с fallback_as_varchar
        приёмник берёт varchar, с column_types — создаёт колонку типом mood."""
        await fill(postgres, "s_copy_en", "mood", "'sad'::mood")
        await load(
            postgres,
            "s_copy_en",
            "t_copy_en",
            mode=StreamWire.CSV,
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        await load(
            postgres,
            "s_copy_en",
            "t_copy_en_typed",
            mode=StreamWire.CSV,
            rules=ColumnRules(column_types={"v": f"{S}.mood"}),
        )

        assert (await Loaded(postgres, S, "t_copy_en").columns())[1] == (
            "v",
            "character varying",
            False,
        )
        assert (await Loaded(postgres, S, "t_copy_en_typed").columns())[1] == (
            "v",
            f"{S}.mood",
            False,
        )
        assert (await Loaded(postgres, S, "t_copy_en_typed").texts("v"))[0] == "sad"

    async def test_copy_mode_applies_the_schema_strategies_too(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_copy_bk", "numeric(18,4)", "g / 7.0")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_copy_bk",
                f"create table {S}.t_copy_bk (id bigint not null, v numeric(10,2))",
            ]
        )
        with pytest.raises(TransferError, match="truncates the scale"):
            await load(
                postgres,
                "s_copy_bk",
                "t_copy_bk",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                mode=StreamWire.CSV,
            )

        report = await load(
            postgres,
            "s_copy_bk",
            "t_copy_bk",
            DropAndCreateIfSchemaChanged(kind="drop_and_create_if_schema_changed"),
            mode=StreamWire.CSV,
        )

        assert "schema: drop_then_create" in report
        assert (await Loaded(postgres, S, "t_copy_bk").columns())[1] == (
            "v",
            "numeric(18,4)",
            False,
        )


EXTENSION_CASES = [
    Case("hstore", "hstore", "hstore('k' || g, 'v \"q\" \\ back')"),
    Case("ltree", "ltree", "('top.node' || g)::ltree"),
    Case("citext", "citext", "('MiXeD' || g)::citext"),
]
"""Типы расширений: реестр psycopg их не знает, тип печатает сервер."""


class TestUnknownTypes:
    """Тип вне встроенной таблицы psycopg (расширение, как vector у pgvector):
    протокол отдаёт только OID, контракт несёт семейство other без имени,
    значения едут текстом; column_types у приёмника даёт точный тип,
    fallback_as_varchar — varchar, fail_on_unknown (умолчание) — ошибка с тем,
    что известно о типе: OID или объявленное у источника имя."""

    @pytest.fixture(autouse=True)
    async def extensions(self, postgres: PostgresSide) -> None:
        if postgres.version < 90100:
            pytest.skip("extensions need 9.1")

        await postgres.execute(
            [
                "create extension if not exists hstore",
                "create extension if not exists ltree",
                "create extension if not exists citext",
            ]
        )

    @pytest.mark.parametrize("case", EXTENSION_CASES, ids=lambda c: c.name)
    @pytest.mark.parametrize(
        "mode", [StreamWire.ARROW, StreamWire.CSV], ids=["arrow", "copy"]
    )
    async def test_extension_type_lands_as_varchar_or_as_declared(
        self, postgres: PostgresSide, case: Case, mode: StreamWire
    ) -> None:
        name = f"{case.name}_{mode.value}"
        await fill(postgres, f"s_{name}", case.kind, case.expr)
        await load(
            postgres,
            f"s_{name}",
            f"t_{name}",
            mode=mode,
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        await load(
            postgres,
            f"s_{name}",
            f"t_{name}_typed",
            mode=mode,
            rules=ColumnRules(column_types={"v": f"{S}.{case.kind}"}),
        )
        source = Loaded(postgres, S, f"s_{name}")
        plain = Loaded(postgres, S, f"t_{name}")
        typed = Loaded(postgres, S, f"t_{name}_typed")

        assert (await plain.columns())[1] == ("v", "character varying", False)
        assert (await typed.columns())[1][1] == (await source.columns())[1][1]
        assert await plain.texts("v") == await source.texts("v")
        assert await typed.texts("v") == await source.texts("v")

    async def test_default_is_refused_with_what_is_known(
        self, postgres: PostgresSide
    ) -> None:
        """Без деклараций — ошибка с голым OID от протокола; source_type,
        объявленный LLM у источника, входит в контракт и для pg -> pg
        становится типом колонки."""
        await fill(postgres, "s_hs_asis", "hstore", "hstore('k', 'v')")
        by_oid = r"column v: the target cannot map the source type oid \d+"
        with pytest.raises(TransferError, match=by_oid):
            await load(postgres, "s_hs_asis", "t_hs_asis", mode=StreamWire.CSV)

        await load(
            postgres,
            "s_hs_asis",
            "t_hs_asis",
            mode=StreamWire.CSV,
            columns=[PgColumnDeclaration(name="v", type_text=f"{S}.hstore")],
        )
        source = Loaded(postgres, S, "s_hs_asis")
        target = Loaded(postgres, S, "t_hs_asis")

        assert (await target.columns())[1][1] == (await source.columns())[1][1]
        assert await target.texts("v") == await source.texts("v")

    async def test_column_types_for_a_missing_column_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_ct_miss", "int", "g")
        with pytest.raises(TransferError, match="column_types: neither the stream"):
            await load(
                postgres,
                "s_ct_miss",
                "t_ct_miss",
                rules=ColumnRules(column_types={"nope": "text"}),
            )


@pytest.fixture(scope="module")
async def vector_db() -> AsyncIterator[PostgresSide]:
    """База приложения (ix_stand) с pgvector: стендовые контейнеры насосов
    расширения vector не имеют."""
    side = PostgresSide(PgSource(name="ix", postgres=IxStand.required().ix_profile), S)
    await side.connect()
    await side.recreate_schema(["create extension if not exists vector"])
    yield side
    await side.drop()


class TestVectorTypes:
    """pgvector: тип вне реестра psycopg — контракт несёт только OID,
    значения едут текстом без разбора; тип колонки задают column_types у
    приёмника или source_type у источника, fallback_as_varchar — varchar."""

    @pytest.mark.parametrize(
        "mode", [StreamWire.ARROW, StreamWire.CSV], ids=["arrow", "copy"]
    )
    async def test_vector_lands_as_vector(
        self, vector_db: PostgresSide, mode: StreamWire
    ) -> None:
        name = f"vec_{mode.value}"
        await vector_db.execute(
            [
                f"drop table if exists {S}.s_{name}",
                f"create table {S}.s_{name} "
                "(id bigint not null, v vector(3), h halfvec(2))",
                f"insert into {S}.s_{name} select g, ('[' || g || ',0.5,-1]')::vector, "
                "'[1,2]'::halfvec from generate_series(1, 3) g "
                "union all select 4, null, null",
            ]
        )
        report = await load(
            vector_db,
            f"s_{name}",
            f"t_{name}",
            mode=mode,
            rules=ColumnRules(column_types={"v": "vector(3)", "h": "halfvec(2)"}),
        )
        source = Loaded(vector_db, S, f"s_{name}")
        target = Loaded(vector_db, S, f"t_{name}")

        assert report.startswith("4 rows written")
        assert (await target.columns())[1:] == [
            ("v", "vector(3)", False),
            ("h", "halfvec(2)", False),
        ]
        assert await target.texts("v") == await source.texts("v")
        assert (await target.texts("v"))[3] is None

    async def test_varchar_fallback_keeps_the_vector_text(
        self, vector_db: PostgresSide
    ) -> None:
        await vector_db.execute(
            [
                f"drop table if exists {S}.s_vec_ddl",
                f"create table {S}.s_vec_ddl (id bigint not null, v vector(3))",
                f"insert into {S}.s_vec_ddl values (1, '[1,2,3]')",
            ]
        )
        await load(
            vector_db,
            "s_vec_ddl",
            "t_vec_ddl",
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        loaded = Loaded(vector_db, S, "t_vec_ddl")

        assert (await loaded.columns())[1] == ("v", "character varying", False)
        assert (await loaded.texts("v"))[0] == "[1,2,3]"


class TestBackupNames:
    async def test_two_backups_in_a_row_get_distinct_names(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        await fill(postgres, "s_bk", "int", "g")
        await postgres.execute(
            [
                f"drop table if exists {S}.t_bk",
                f"create table {S}.t_bk (id bigint not null, old text)",
            ]
        )
        from boba.toolkit.transfer import BackupAndCreate

        first = await load(
            postgres, "s_bk", "t_bk", BackupAndCreate(kind="backup_and_create")
        )
        second = await load(
            postgres, "s_bk", "t_bk", BackupAndCreate(kind="backup_and_create")
        )
        backups = [
            t
            for t in await Loaded(postgres, S, "t_bk").tables()
            if t.startswith("t_bk_bak_")
        ]

        assert "backup: t_bk_bak_" in first
        assert "backup: t_bk_bak_" in second
        assert len(backups) == 2
