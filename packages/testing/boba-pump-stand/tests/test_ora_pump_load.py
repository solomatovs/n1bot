"""Нагрузка на перекачку Oracle -> postgres и Oracle -> ClickHouse: миллион
строк типичной широкой таблицы (числа, double, строки с NULL, DATE,
TIMESTAMP, TIMESTAMP WITH TIME ZONE, RAW) едут насосом ora_csv_out через
трубу ОС в pg_stream_in или ch_stream_in, оба насоса работают одновременно.

Источники — новейший Oracle и 12.2 Enterprise, приёмники — новейшие postgres
и ClickHouse. Проверяется, что строки и агрегаты сходятся с источником, и
что поток не копится в памяти: прирост пика RSS процесса за перекачку
ограничен, хотя данных в разы больше. Скорость печатается в вывод теста."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.oracle.payload import PayloadOracle
from boba.db.postgres import AsyncPostgresPool
from boba.pump_stand import ChSource, OracleStand, OraSource, PgSource, Pumps, PumpStand
from boba.pump_stand.oracle import PumpUser

pytestmark = [pytest.mark.integration, pytest.mark.load, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 1_000_000
CHUNK_BYTES = 1 << 18
PEAK_GROWTH_MIB = 256
"""Потолок прироста пика RSS за перекачку: CSV миллиона строк в разы больше."""
PG_SCHEMA = "pump_ora_load"
CH_DATABASE = "pump_ora_load"
ORA_TABLE = f"{PumpUser.NAME}.load"


class Memory:
    """Пик RSS процесса: clear_refs сбрасывает VmHWM к текущему RSS, после
    перекачки VmHWM — её пик."""

    STATUS: ClassVar[Path] = Path("/proc/self/status")
    CLEAR: ClassVar[Path] = Path("/proc/self/clear_refs")
    RESET_PEAK: ClassVar[str] = "5"

    def reset(self) -> int:
        self.CLEAR.write_text(self.RESET_PEAK)

        return self._field("VmRSS:")

    def peak(self) -> int:
        return self._field("VmHWM:")

    def _field(self, name: str) -> int:
        for line in self.STATUS.read_text().splitlines():
            if not line.startswith(name):
                continue

            _, amount, _ = line.split()

            return int(amount) // 1024

        raise AssertionError(f"{self.STATUS}: no {name}")


@dataclass(frozen=True)
class Totals:
    """Агрегаты таблицы, одинаково посчитанные на каждой стороне."""

    rows: int
    n18_4: Decimal
    n38_10: Decimal
    vn: int
    vc_length: int
    latest: str


class Oracle:
    """Таблица load в схеме стенда: миллион строк декартовым произведением."""

    DDL: ClassVar[str] = (
        "create table load ("
        " id number(10) not null,"
        " n18_4 number(18, 4) not null,"
        " n38_10 number(38, 10) not null,"
        " bd binary_double not null,"
        " vc varchar2(100) not null,"
        " vn varchar2(40),"
        " dt date not null,"
        " ts6 timestamp(6) not null,"
        " tstz timestamp(6) with time zone not null,"
        " rw16 raw(16) not null)"
    )
    FILL: ClassVar[str] = (
        "insert /*+ append */ into load"
        " select g, g / 3, g * 1234567.123456789 - 99999999999.5, g / 7,"
        " 'row ' || g || ', \"quoted\" text' || chr(10) || 'line',"
        " case when mod(g, 5) = 0 then null else 'n' || g end,"
        " date '2000-01-01' + g / 1440,"
        " timestamp '2000-01-01 00:00:00.123456' + numtodsinterval(g, 'second'),"
        " from_tz(timestamp '2020-01-01 00:00:00' + numtodsinterval(g, 'second'),"
        " '+03:00'),"
        " standard_hash(to_char(g), 'MD5')"
        " from (select (a.g - 1) * 1000 + b.g as g"
        " from (select level as g from dual connect by level <= 1000) a"
        " cross join (select level as g from dual connect by level <= 1000) b)"
    )
    TOTALS: ClassVar[str] = (
        f"select count(*), sum(n18_4), sum(n38_10), count(vn), sum(length(vc)), "
        f"to_char(max(ts6), 'yyyy-mm-dd hh24:mi:ss.ff6') from {ORA_TABLE}"
    )

    def __init__(self, source: OraSource) -> None:
        self.source = source
        self.stand = OracleStand(source)

    async def recreate(self) -> None:
        await self.stand.recreate_user()
        await self.stand.run((self.DDL,))
        await self.stand.run((self.FILL,))

    async def totals(self) -> Totals:
        payload = PayloadOracle(self.stand.owner)
        async with (
            payload.opened() as conn,
            payload.rows(conn, self.TOTALS) as stream,
        ):
            rows = [row async for row in stream.blocks]

        return _totals(rows[0])

    async def drop(self) -> None:
        await self.stand.drop()


class Postgres:
    """Приёмник postgres: таблица под load и её агрегаты."""

    DDL: ClassVar[str] = (
        f"create table {PG_SCHEMA}.load (id bigint, n18_4 numeric(18, 4), "
        "n38_10 numeric(38, 10), bd double precision, vc text, vn text, "
        "dt timestamp(0), ts6 timestamp(6), tstz timestamptz, rw16 uuid)"
    )
    TOTALS: ClassVar[str] = (
        "select count(*), sum(n18_4), sum(n38_10), count(vn), sum(length(vc)), "
        f"to_char(max(ts6), 'YYYY-MM-DD HH24:MI:SS.US') from {PG_SCHEMA}.load"
    )

    def __init__(self, source: PgSource) -> None:
        self.source = source

    async def recreate(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))
            await conn.execute(self._q(f"create schema {PG_SCHEMA}"))
            await conn.execute(self._q(self.DDL))

    async def totals(self) -> Totals:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            cursor = await conn.execute(self._q(self.TOTALS))
            row = await cursor.fetchone()

        if row is None:
            raise AssertionError("totals returned no row")

        return _totals(row)

    async def drop(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))

    def _q(self, text: str) -> bytes:
        return text.encode()


class ClickHouse:
    """Приёмник ClickHouse: таблица под load, вставка через input() и агрегаты."""

    DDL: ClassVar[str] = (
        f"create table {CH_DATABASE}.load (id Int64, n18_4 Decimal(18, 4), "
        "n38_10 Decimal(38, 10), bd Float64, vc String, vn Nullable(String), "
        "dt DateTime('UTC'), ts6 DateTime64(6, 'UTC'), "
        "tstz DateTime64(6, 'UTC'), rw16 UUID) engine = MergeTree order by id"
    )
    STRUCTURE: ClassVar[str] = (
        "id Int64, n18_4 Decimal(18, 4), n38_10 Decimal(38, 10), bd String, "
        "vc String, vn Nullable(String), dt DateTime(''UTC''), "
        "ts6 DateTime64(6, ''UTC''), tstz DateTime64(6, ''UTC''), rw16 UUID"
    )
    TOTALS: ClassVar[str] = (
        "select count(), sum(n18_4), sum(n38_10), count(vn), sum(length(vc)), "
        f"toString(max(ts6)) from {CH_DATABASE}.load"
    )

    def __init__(self, source: ChSource) -> None:
        self.source = source

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {CH_DATABASE}")
            await client.command(f"create database {CH_DATABASE}")
            await client.command(self.DDL)

    def insert(self) -> str:
        return (
            f"insert into {CH_DATABASE}.load select id, n18_4, n38_10, "
            "toFloat64(bd), vc, vn, dt, ts6, tstz, rw16 "
            f"from input('{self.STRUCTURE}') "
            "settings precise_float_parsing = 1 format CSV"
        )

    async def totals(self) -> Totals:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query(self.TOTALS)

        return _totals(result.result_rows[0])

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {CH_DATABASE}")


def _totals(row: Sequence[Any]) -> Totals:
    return Totals(
        rows=int(row[0]),
        n18_4=Decimal(str(row[1])),
        n38_10=Decimal(str(row[2])),
        vn=int(row[3]),
        vc_length=int(row[4]),
        latest=str(row[5]),
    )


EXPORT = (
    "select id, n18_4, n38_10, bd, vc, vn, dt, ts6, "
    "to_char(tstz, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm') as tstz, "
    f"rawtohex(rw16) as rw16 from {ORA_TABLE}"
)


def _loaded(sources: Sequence[OraSource]) -> list[OraSource]:
    """Новейший Oracle и 12.2 Enterprise: у XE лимит на процессор и память."""
    return [sources[-1], sources[0]]


def _newest_postgres(sources: Sequence[PgSource]) -> PgSource:
    plain: list[PgSource] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source)

    return plain[-1]


@pytest.fixture(scope="module", params=_loaded(STAND.ora_sources), ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[Oracle]:
    side = Oracle(request.param)
    await side.recreate()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def postgres() -> AsyncIterator[Postgres]:
    side = Postgres(_newest_postgres(STAND.sources))
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ClickHouse]:
    side = ClickHouse(STAND.demo_clickhouse()[-1])
    yield side
    await side.drop()


class TestLoad:
    async def test_million_rows_into_postgres(
        self, oracle: Oracle, postgres: Postgres
    ) -> None:
        await postgres.recreate()
        pumps = Pumps(postgres=postgres.source.postgres, oracle=oracle.stand.owner)
        memory = Memory()
        baseline = memory.reset()

        chained = await pumps.chain(
            "ora_csv_out",
            EXPORT,
            "pg_stream_in",
            f"copy {PG_SCHEMA}.load from stdin (format csv)",
            CHUNK_BYTES,
        )

        growth = memory.peak() - baseline
        print(
            f"\n{oracle.source.name} -> {postgres.source.name}: {ROWS} rows in "
            f"{chained.seconds:.1f}s, {ROWS / chained.seconds:,.0f} rows/s, "
            f"{chained.out_report}, peak rss +{growth} MiB"
        )

        assert chained.in_report == f"server: COPY {ROWS}"
        assert await postgres.totals() == await oracle.totals()
        assert growth < PEAK_GROWTH_MIB

    async def test_million_rows_into_clickhouse(
        self, oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        await clickhouse.recreate()
        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.stand.owner)
        memory = Memory()
        baseline = memory.reset()

        chained = await pumps.chain(
            "ora_csv_out", EXPORT, "ch_stream_in", clickhouse.insert(), CHUNK_BYTES
        )

        growth = memory.peak() - baseline
        print(
            f"\n{oracle.source.name} -> {clickhouse.source.name}: {ROWS} rows in "
            f"{chained.seconds:.1f}s, {ROWS / chained.seconds:,.0f} rows/s, "
            f"{chained.out_report}, peak rss +{growth} MiB"
        )

        assert chained.in_report == f"{ROWS} rows written"
        assert await clickhouse.totals() == await oracle.totals()
        assert growth < PEAK_GROWTH_MIB
