"""Цепочка ClickHouse -> postgres -> ClickHouse насосами ch_stream_out,
COPY postgres и ch_stream_in: COPY текстом и TabSeparated совпадают по
экранированию и NULL (\\N с обеих сторон), байты идут без преобразования."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from psycopg import sql

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.query import PgQueryBuilder
from boba.pump_stand import ChSource, Pumps, PumpStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 5000
PG_TABLE = sql.Identifier("ch_stream_probe")


class Stand:
    """База стенда насосов на источнике: таблица customers с NULL, пустая
    таблица-приёмник с колонками в другом порядке; после модуля сносится."""

    DATABASE: ClassVar[str] = "stream_stand"

    def __init__(self, source: ChSource) -> None:
        self._source = source

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists $db")
            await self._command(client, "create database $db")
            await self._command(
                client,
                "create table $db.customers (id UInt64, email String, "
                "note Nullable(String)) engine = MergeTree order by id",
            )
            await self._command(
                client,
                "create table $db.sink (note Nullable(String), email String, "
                "id UInt64) engine = MergeTree order by id",
            )
            await self._command(
                client,
                "insert into $db.customers select number, "
                "concat('user', toString(number), '@example.com'), "
                "if(number % 3 = 0, null, concat('o''neil\\t', toString(number))) "
                "from numbers($rows)",
                rows=str(ROWS),
            )

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists $db")

    async def fingerprint(self, table: str) -> tuple[Any, ...]:
        query = (
            ChQueryBuilder()
            .add(
                "select count(), count(note), sum(id), sum(length(email)), "
                "sum(length(note)), max(note) from $db.$t",
                db=self.DATABASE,
                t=table,
            )
            .build()
        )
        async with (
            PayloadClickHouse.opened_config(self._source.admin) as client,
            PayloadClickHouse.rows_stream_out(client, query.text) as stream,
        ):
            rows = [tuple(row) async for row in stream.blocks]

        return rows[0]

    async def truncate(self, table: str) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "truncate table $db.$t", t=table)

    async def _command(self, client: Any, text: str, **bind: str) -> None:
        query = ChQueryBuilder().add(text, db=self.DATABASE, **bind).build()
        await client.command(query.text)


def _pg(text: str) -> sql.Composed:
    return PgQueryBuilder(table=PG_TABLE).add(text).build().text


def _ch(text: str, **bind: str) -> str:
    """Стейтмент для насоса: имена стенда подставляются голым текстом."""
    return ChQueryBuilder().add(text, db=Stand.DATABASE, **bind).build().text


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
def source(request: Any) -> ChSource:
    return request.param


@pytest.fixture(scope="module")
async def stand(source: ChSource) -> AsyncIterator[Stand]:
    made = Stand(source)
    await made.recreate()
    yield made
    await made.drop()


class TestClickHouseToPostgres:
    async def test_tsv_matches_copy_text(self, stand: Stand, source: ChSource) -> None:
        """COPY текстом и TabSeparated совпадают по экранированию и NULL, так
        что байты идут в обе стороны без преобразования."""
        pumps = Pumps(clickhouse=source.admin)
        await stand.truncate("sink")

        exported = await pumps.ch_out(
            _ch(
                "select id, email, note from $db.customers order by id "
                "format TabSeparated"
            )
        )

        async with await AsyncPostgresPool.dedicated(STAND.ix_profile) as pg:
            await pg.execute(_pg("drop table if exists {table}"))
            await pg.execute(
                _pg("create table {table} (id bigint, email text, note text)")
            )
            async with (
                pg.cursor() as cur,
                cur.copy(_pg("copy {table} from stdin")) as copy,
            ):
                await copy.write(exported)

            counted = await pg.execute(_pg("select count(*), count(note) from {table}"))
            assert await counted.fetchone() == (ROWS, ROWS - (ROWS + 2) // 3)

            landed = bytearray()
            async with (
                pg.cursor() as cur,
                cur.copy(
                    _pg(
                        "copy (select note, email, id from {table} order by id) "
                        "to stdout"
                    )
                ) as copy,
            ):
                async for block in copy:
                    landed.extend(block)

            await pg.execute(_pg("drop table {table}"))

        report = await pumps.ch_in(
            _ch("insert into $db.sink (note, email, id) format TabSeparated"),
            bytes(landed),
        )

        assert f"{ROWS} rows written" in report
        assert await stand.fingerprint("sink") == await stand.fingerprint("customers")
