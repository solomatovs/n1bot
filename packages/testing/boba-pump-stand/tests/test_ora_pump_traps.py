"""Ловушки перекачки Oracle -> postgres и Oracle -> ClickHouse: на каждой
запрос, написанный в лоб, теряет данные или падает, а рядом записан
правильный вариант. Всё идёт через тела насосов ora_csv_out, pg_stream_in и
ch_stream_in, как в графе workflow.

Сторона Oracle проверяется на всех версиях стенда выборками из dual: типы,
которые выгрузка отвергает (NUMBER без точности с дробью, RAW, INTERVAL,
XMLTYPE, JSON, VECTOR, именованный пояс, дата до нашей эры), и типы, которые
едут молча с потерей (TIMESTAMP WITH TIME ZONE без смещения, TIMESTAMP(9)
до микросекунд, FLOAT(126) через double). Сторона postgres — на старейшем
и новейшем сервере, сторона ClickHouse — на всех версиях стенда."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.oracle import OracleQueryError
from boba.db.postgres import AsyncPostgresPool
from boba.pump_stand import ChSource, OraSource, PgSource, Pumps, PumpStand
from boba.pump_stand.oracle import OracleStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = "pump_ora_trap"
CH_DATABASE = "pump_ora_trap"


@dataclass(frozen=True)
class Refused:
    """Выражение, которое ora_csv_out отвергает, и его правильная замена с
    точным CSV на выходе."""

    name: str
    wrong: str
    error: str
    right: str
    csv: bytes
    min_version: int = 12


REFUSED = (
    Refused(
        "number_without_precision",
        "cast(1/7 as number)",
        "DPY-4042",
        "to_char(cast(1/7 as number), 'TM9')",
        b'".1428571428571428571428571428571428571429"\n',
    ),
    Refused(
        "raw",
        "hextoraw('00FF')",
        "binary needs rawtohex",
        "rawtohex(hextoraw('00FF'))",
        b'"00FF"\n',
    ),
    Refused(
        "interval_day_to_second",
        "interval '3 04:05:06' day to second",
        "DPY-3038",
        "to_char(interval '3 04:05:06' day to second)",
        b'"+03 04:05:06.000000"\n',
    ),
    Refused(
        "interval_year_to_month",
        "interval '-1-2' year to month",
        "DPY-3038",
        "to_char(interval '-1-2' year to month)",
        b'"-01-02"\n',
    ),
    Refused(
        "xmltype",
        "xmltype('<a b=\"1\"/>')",
        "DPY-3030",
        "xmlserialize(document xmltype('<a b=\"1\"/>') as clob)",
        b'"<a b=""1""/>"\n',
    ),
    Refused(
        "named_time_zone",
        "to_timestamp_tz('2024-02-29 13:14:15 Europe/Moscow', "
        "'yyyy-mm-dd hh24:mi:ss tzr')",
        "DPY-3022",
        "to_char(to_timestamp_tz('2024-02-29 13:14:15 Europe/Moscow', "
        "'yyyy-mm-dd hh24:mi:ss tzr'), 'yyyy-mm-dd hh24:mi:sstzh:tzm')",
        b'"2024-02-29 13:14:15+03:00"\n',
    ),
    Refused(
        "date_before_year_one",
        "to_date('-4000-01-01', 'syyyy-mm-dd')",
        "year -4000 is out of range",
        "to_char(to_date('-4000-01-01', 'syyyy-mm-dd'), 'syyyy-mm-dd')",
        b'"-4000-01-01"\n',
    ),
    Refused(
        "json",
        "json('{\"a\": 1}')",
        "DPY-3030",
        "json_serialize(json('{\"a\": 1}') returning clob)",
        b'"{""a"":1}"\n',
        min_version=21,
    ),
    Refused(
        "vector",
        "to_vector('[105, 1.5]')",
        "DPY-3031",
        "from_vector(to_vector('[105, 1.5]', 2, float32))",
        b'"[1.05E+002,1.5E+000]"\n',
        min_version=23,
    ),
)


@dataclass(frozen=True)
class Silent:
    """Выражение, которое едет без ошибки, но с потерей, и правильный вариант."""

    name: str
    wrong: str
    wrong_csv: bytes
    right: str
    right_csv: bytes


SILENT = (
    Silent(
        "timestamp_with_time_zone_drops_the_offset",
        "from_tz(timestamp '2024-02-29 13:14:15.123456', '+03:00')",
        b"2024-02-29 13:14:15.123456000\n",
        "to_char(from_tz(timestamp '2024-02-29 13:14:15.123456', '+03:00'), "
        "'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')",
        b'"2024-02-29 13:14:15.123456+03:00"\n',
    ),
    Silent(
        "timestamp9_drops_nanoseconds",
        "cast(timestamp '2024-02-29 13:14:15.123456789' as timestamp(9))",
        b"2024-02-29 13:14:15.123456000\n",
        "to_char(cast(timestamp '2024-02-29 13:14:15.123456789' as timestamp(9)), "
        "'yyyy-mm-dd hh24:mi:ss.ff9')",
        b'"2024-02-29 13:14:15.123456789"\n',
    ),
    Silent(
        "float126_goes_as_double",
        "cast(1/3 as float(126))",
        b"0.3333333333333333\n",
        "to_char(cast(1/3 as float(126)), 'TM9')",
        b'".33333333333333333333333333333333333333"\n',
    ),
    Silent(
        "empty_string_is_null",
        "cast('' as varchar2(5))",
        b"\n",
        "nvl(cast('' as varchar2(5)), '-')",
        b'"-"\n',
    ),
)


class Oracle:
    """Сторона Oracle ловушек: выгрузка выборки из dual администратором —
    схемы не нужны, стенд матрицы не задевается."""

    def __init__(self, source: OraSource) -> None:
        self.source = source
        self.pumps = Pumps(oracle=source.admin)
        self.version = 0

    async def connect(self) -> None:
        self.version = await OracleStand(self.source).version()

    async def out(self, expression: str) -> bytes:
        return await self.pumps.ora_out(f"select {expression} from dual")


class Postgres:
    """Сторона postgres: одноколоночная таблица под каждую ловушку."""

    def __init__(self, source: PgSource) -> None:
        self.source = source

    async def land(self, pg_type: str, data: bytes) -> str | None:
        """Байты выгрузки в колонку pg_type через pg_stream_in; значение текстом."""
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))
            await conn.execute(self._q(f"create schema {PG_SCHEMA}"))
            await conn.execute(self._q(f"create table {PG_SCHEMA}.t (v {pg_type})"))

        pumps = Pumps(postgres=self.source.postgres)
        await pumps.pg_in(f"copy {PG_SCHEMA}.t from stdin (format csv)", data)

        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            cursor = await conn.execute(self._q(f"select v::text from {PG_SCHEMA}.t"))
            row = await cursor.fetchone()
            await conn.execute(self._q(f"drop schema {PG_SCHEMA} cascade"))

        if row is None:
            raise AssertionError("the trap table is empty")

        return row[0]

    def _q(self, text: str) -> bytes:
        return text.encode()


class ClickHouse:
    """Сторона ClickHouse: одноколоночная таблица под каждую ловушку."""

    def __init__(self, source: ChSource) -> None:
        self.source = source
        self.precise_floats = False

    async def connect(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            found = await client.query(
                "select count() from system.settings "
                "where name = 'precise_float_parsing'"
            )

        self.precise_floats = found.result_rows[0][0] == 1

    async def land(self, ch_type: str, data: bytes, insert: str = "") -> str | None:
        """Байты выгрузки в колонку ch_type через ch_stream_in; значение текстом.
        insert — свой стейтмент вставки вместо прямого FORMAT CSV."""
        statement = insert
        if not statement:
            statement = f"insert into {CH_DATABASE}.t format CSV"

        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {CH_DATABASE}")
            await client.command(f"create database {CH_DATABASE}")
            await client.command(
                f"create table {CH_DATABASE}.t (v {ch_type}) engine = Memory"
            )

        pumps = Pumps(clickhouse=self.source.admin)
        await pumps.ch_in(statement, data)

        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query(f"select toString(v) from {CH_DATABASE}.t")
            await client.command(f"drop database {CH_DATABASE}")

        return result.result_rows[0][0]

    def settings(self) -> str:
        if self.precise_floats:
            return " settings precise_float_parsing = 1"

        return ""


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> Oracle:
    side = Oracle(request.param)
    await side.connect()

    return side


@pytest.fixture(scope="module")
async def newest_oracle() -> Oracle:
    side = Oracle(STAND.ora_sources[-1])
    await side.connect()

    return side


def _edges(sources: Sequence[PgSource]) -> list[PgSource]:
    """Старейший и новейший postgres стенда (Greenplum не в счёт)."""
    plain: list[PgSource] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source)

    return [plain[0], plain[-1]]


@pytest.fixture(scope="module", params=_edges(STAND.sources), ids=lambda s: s.name)
async def postgres(request: Any) -> Postgres:
    return Postgres(request.param)


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> ClickHouse:
    side = ClickHouse(request.param)
    await side.connect()

    return side


class TestOracleSide:
    @pytest.mark.parametrize("case", REFUSED, ids=lambda c: c.name)
    async def test_refused_type_needs_a_conversion(
        self, oracle: Oracle, case: Refused
    ) -> None:
        if oracle.version < case.min_version:
            pytest.skip(f"oracle {oracle.version} has no such type")

        with pytest.raises(OracleQueryError, match=case.error):
            await oracle.out(case.wrong)

        assert await oracle.out(case.right) == case.csv

    @pytest.mark.parametrize("case", SILENT, ids=lambda c: c.name)
    async def test_silent_loss_and_its_fix(self, oracle: Oracle, case: Silent) -> None:
        assert await oracle.out(case.wrong) == case.wrong_csv
        assert await oracle.out(case.right) == case.right_csv

    async def test_json_function_before_21_is_silently_null(
        self, oracle: Oracle
    ) -> None:
        """До 21c типа JSON нет, а json(...) молча даёт NULL, а не ошибку."""
        if oracle.version >= 21:
            pytest.skip("oracle 21+ has the JSON type")

        assert await oracle.out("json('{\"a\": 1}')") == b"\n"


class TestPostgresSide:
    async def test_negative_interval_text_flips_the_sign_of_the_time(
        self, newest_oracle: Oracle, postgres: Postgres
    ) -> None:
        """to_char(INTERVAL DAY TO SECOND) пишет знак один раз на всё значение,
        а postgres относит его только к дням: -(11 дней 13:46:40) становится
        -11 дней +13:46:40. Правильно — секунды числом."""
        interval = "numtodsinterval(-1000000.5, 'second')"
        seconds = (
            f"to_char(extract(day from {interval}) * 86400 "
            f"+ extract(hour from {interval}) * 3600 "
            f"+ extract(minute from {interval}) * 60 "
            f"+ extract(second from {interval}), 'TM9')"
        )

        wrong = await newest_oracle.out(f"to_char({interval})")
        right = await newest_oracle.out(seconds)

        assert await postgres.land("interval", wrong) == "-11 days +13:46:40.5"
        assert await postgres.land("interval", right) == "-277:46:40.5"

    async def test_nanoseconds_round_to_even_in_postgres(
        self, newest_oracle: Oracle, postgres: Postgres
    ) -> None:
        """Половина микросекунды: postgres округляет текст к чётному, Oracle при
        cast в timestamp(6) — вверх. Правильно — округлять на стороне Oracle."""
        value = "timestamp '2000-01-01 00:27:53.123468500'"
        wrong = await newest_oracle.out(
            f"to_char({value}, 'yyyy-mm-dd hh24:mi:ss.ff9')"
        )
        right = await newest_oracle.out(
            f"to_char(cast({value} as timestamp(6)), 'yyyy-mm-dd hh24:mi:ss.ff6')"
        )

        assert (
            await postgres.land("timestamp(6)", wrong) == "2000-01-01 00:27:53.123468"
        )
        assert (
            await postgres.land("timestamp(6)", right) == "2000-01-01 00:27:53.123469"
        )

    async def test_bytea_prefix_turns_null_into_an_empty_value(
        self, newest_oracle: Oracle, postgres: Postgres
    ) -> None:
        """В Oracle '\\x' || NULL это '\\x', а не NULL: postgres получит пустой
        bytea. Префикс ставится только непустому значению."""
        raw = "cast(null as raw(16))"
        wrong = await newest_oracle.out(f"'\\x' || rawtohex({raw})")
        right = await newest_oracle.out(
            f"case when {raw} is not null then '\\x' || rawtohex({raw}) end"
        )

        assert await postgres.land("bytea", wrong) == "\\x"
        assert await postgres.land("bytea", right) is None

    async def test_date_column_drops_the_time_of_an_oracle_date(
        self, newest_oracle: Oracle, postgres: Postgres
    ) -> None:
        """DATE Oracle всегда со временем; колонка date postgres его молча
        отбрасывает, timestamp(0) сохраняет."""
        data = await newest_oracle.out(
            "to_date('2024-02-29 13:14:15', 'yyyy-mm-dd hh24:mi:ss')"
        )

        assert await postgres.land("date", data) == "2024-02-29"
        assert await postgres.land("timestamp(0)", data) == "2024-02-29 13:14:15"

    async def test_extra_decimal_digits_are_rounded(
        self, newest_oracle: Oracle, postgres: Postgres
    ) -> None:
        """numeric(p, s) postgres округляет лишние знаки, ClickHouse — отбрасывает."""
        data = await newest_oracle.out("cast(-0.14286 as number(10, 5))")

        assert await postgres.land("numeric(18, 4)", data) == "-0.1429"


class TestClickHouseSide:
    async def test_extra_decimal_digits_are_truncated(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """Decimal ClickHouse отбрасывает лишние знаки без округления и без
        ошибки. Правильно — округлить в Oracle до масштаба приёмника."""
        wrong = await newest_oracle.out("cast(-0.14286 as number(10, 5))")
        right = await newest_oracle.out("round(cast(-0.14286 as number(10, 5)), 4)")

        assert await clickhouse.land("Decimal(18, 4)", wrong) == "-0.1428"
        assert await clickhouse.land("Decimal(18, 4)", right) == "-0.1429"

    async def test_tiny_number_becomes_zero(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """TM9 пишет крошечные числа экспонентой, Decimal ClickHouse молча
        обращает их в 0."""
        data = await newest_oracle.out("to_char(cast(1e-130 as number), 'TM9')")

        assert data == b'"1E-130"\n'
        assert await clickhouse.land("Decimal(38, 10)", data) == "0"

    async def test_float32_exponent_needs_the_string_path(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """Float32 из текста с экспонентой разбирается неточно (1.05E+002 ->
        104.99999) даже прямо в колонку, а на 22.12 и через toFloat32; через
        input() String, toFloat64 и приведение к Float32 — точно везде."""
        data = await newest_oracle.out("'1.05E+002'")
        precise = (
            f"insert into {CH_DATABASE}.t select toFloat32(toFloat64(v)) "
            f"from input('v String'){clickhouse.settings()} format CSV"
        )

        assert await clickhouse.land("Float32", data) == "104.99999"
        assert await clickhouse.land("Float32", data, precise) == "105"

    async def test_vector_goes_through_json_extract(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """Текст VECTOR (from_vector) в Array(Float32) теряет точность на
        экспоненте; JSONExtract в Float64 и приведение к Float32 — точно."""
        data = await newest_oracle.out("'[1.05E+002,1.5E+000]'")
        extracted = (
            f"insert into {CH_DATABASE}.t "
            "select CAST(JSONExtract(v, 'Array(Float64)'), 'Array(Float32)') "
            "from input('v String') format CSV"
        )

        assert await clickhouse.land("Array(Float32)", data) == "[104.99999,1.5]"
        assert await clickhouse.land("Array(Float32)", data, extracted) == "[105,1.5]"

    async def test_datetime64_clamps_out_of_range_dates(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """DateTime64 держит 1900–2299: даты Oracle вне диапазона молча
        зажимаются в край, date_time_overflow_behavior на вставку не влияет."""
        high = await newest_oracle.out(
            "to_date('9999-12-31 23:59:59', 'yyyy-mm-dd hh24:mi:ss')"
        )
        low = await newest_oracle.out("date '0001-01-01'")

        assert await clickhouse.land("DateTime64(0, 'UTC')", high) == (
            "2299-12-31 23:59:59"
        )
        assert await clickhouse.land("DateTime64(0, 'UTC')", low) == (
            "1900-01-01 00:00:00"
        )

    async def test_datetime32_wraps_or_clamps_after_2106(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """DateTime (32 бита) держит 1970–2106: до 26-й версии 2200 год
        заворачивается в произвольную дату, с 26-й — зажимается в край."""
        data = await newest_oracle.out("date '2200-01-01'")

        landed = await clickhouse.land("DateTime('UTC')", data)

        assert landed in ("2063-11-24 17:31:44", "2106-02-07 06:28:15")

    async def test_date32_needs_a_date_only_text(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """DATE Oracle выгружается со временем, а Date32 из CSV его не принимает;
        to_char(d, 'yyyy-mm-dd') — принимает."""
        wrong = await newest_oracle.out("date '2024-02-29'")
        right = await newest_oracle.out("to_char(date '2024-02-29', 'yyyy-mm-dd')")

        with pytest.raises(ClickHouseQueryError, match="Code: 117"):
            await clickhouse.land("Date32", wrong)

        assert await clickhouse.land("Date32", right) == "2024-02-29"

    async def test_null_lands_as_default_in_a_non_nullable_column(
        self, newest_oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """NULL Oracle — пустое поле CSV; в не-Nullable колонке ClickHouse оно
        молча становится значением по умолчанию, в Nullable — NULL."""
        data = await newest_oracle.out("cast(null as number)")

        assert await clickhouse.land("Int64", data) == "0"
        assert await clickhouse.land("String", data) == ""
        assert await clickhouse.land("Nullable(Int64)", data) is None
