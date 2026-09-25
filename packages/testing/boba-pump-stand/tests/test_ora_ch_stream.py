"""Цепочка Oracle -> ClickHouse насосами ora_csv_out и ch_stream_in: CSV
уезжает как есть, без разбора между узлами; число строк, NULL и суммы
совпадают на обеих сторонах. Oracle — первый источник стенда, ClickHouse —
новейший."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChIdentifier, ChQueryBuilder
from boba.db.oracle import OraIdentifier, OraQueryBuilder
from boba.db.oracle.payload import PayloadOracle
from boba.pump_stand import ChSource, OracleStand, Pumps, PumpStand
from boba.pump_stand.oracle import PumpUser

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 5000

CUSTOMERS = OraIdentifier(f"{PumpUser.NAME}.customers")
SELECT = (
    OraQueryBuilder()
    .add(
        "select id, email, balance, created_at, note, rawtohex(photo) as photo from ",
        CUSTOMERS,
        " order by id",
    )
    .build()
)
FINGERPRINT = (
    OraQueryBuilder()
    .add(
        "select count(*), count(note), count(photo), sum(balance), "
        "sum(length(email)), sum(length(rawtohex(photo))) from ",
        CUSTOMERS,
    )
    .build()
)


class ChTarget:
    """База-приёмник на ClickHouse с таблицей под колонки customers."""

    DATABASE: ClassVar[str] = "ora_pump_stand"
    TABLE: ClassVar[str] = "customers"
    COLUMNS: ClassVar[tuple[str, ...]] = (
        "id",
        "email",
        "balance",
        "created_at",
        "note",
        "photo",
    )

    def __init__(self, source: ChSource) -> None:
        self.source = source

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await self._command(client, "drop database if exists %(db)s")
            await self._command(client, "create database %(db)s")
            await self._command(
                client,
                "create table %(db)s.%(t)s (id UInt64, email String, "
                "balance Decimal(18, 2), created_at DateTime64(6), "
                "note Nullable(String), photo Nullable(String)) "
                "engine = MergeTree order by id",
                t=ChIdentifier(self.TABLE),
            )

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await self._command(client, "drop database if exists %(db)s")

    async def fingerprint(self) -> tuple[Any, ...]:
        query = (
            ChQueryBuilder()
            .add(
                "select count(), count(note), count(photo), sum(balance), "
                "sum(length(email)), sum(length(photo)) from %(db)s.%(t)s",
                db=ChIdentifier(self.DATABASE),
                t=ChIdentifier(self.TABLE),
            )
            .build()
        )
        async with (
            PayloadClickHouse.opened_config(self.source.admin) as client,
            PayloadClickHouse.rows_stream_out(
                client, query.text, query.params
            ) as stream,
        ):
            rows = [tuple(row) async for row in stream.blocks]

        return rows[0]

    async def _command(self, client: Any, text: str, **bind: Any) -> None:
        query = (
            ChQueryBuilder().add(text, db=ChIdentifier(self.DATABASE), **bind).build()
        )
        await client.command(query.text, parameters=query.params)


async def _ora_fingerprint(stand: OracleStand) -> tuple[Any, ...]:
    payload = PayloadOracle(stand.owner)
    async with payload.opened() as conn, payload.rows(conn, FINGERPRINT.text) as stream:
        rows = [tuple(row) async for row in stream.blocks]

    return rows[0]


def _normalized(row: Sequence[Any]) -> tuple[Any, ...]:
    """Числа обеих сторон к одному виду: счётчики int, суммы Decimal."""
    listed: list[Any] = []
    for value in row:
        if isinstance(value, float):
            listed.append(Decimal(str(value)))
            continue

        if isinstance(value, Decimal):
            listed.append(value.quantize(Decimal("0.01")))
            continue

        listed.append(int(value))

    return tuple(listed)


@pytest.fixture(scope="module")
async def oracle() -> AsyncIterator[OracleStand]:
    stand = OracleStand(STAND.ora_sources[0])
    await stand.recreate(ROWS)
    yield stand
    await stand.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ChTarget]:
    target = ChTarget(STAND.demo_clickhouse()[-1])
    await target.recreate()
    yield target
    await target.drop()


class TestOracleToClickHouse:
    async def test_csv_goes_through_untouched(
        self, oracle: OracleStand, clickhouse: ChTarget
    ) -> None:
        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.owner)

        exported = await pumps.ora_out(SELECT.text)
        assert exported.count(b"\n") == ROWS

        insert = (
            ChQueryBuilder()
            .add(
                "insert into ",
                ChTarget.DATABASE,
                ".",
                ChTarget.TABLE,
                " (",
                ", ".join(ChTarget.COLUMNS),
                ") format CSV",
            )
            .build()
        )
        report = await pumps.ch_in(insert.text, exported)
        assert f"{ROWS} rows written" in report

        assert _normalized(await clickhouse.fingerprint()) == _normalized(
            await _ora_fingerprint(oracle)
        )
