"""Насосы перекачки на живом стенде: ora_copy_out отдаёт CSV, который postgres
принимает COPY как есть; ora_copy_in принимает CSV из COPY postgres с NULL как \\N;
круг Oracle -> postgres -> Oracle не теряет ни строк, ни NULL, ни байтов RAW."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
from ora_tool_stand import DemoUser, IxStand, ToolDemo
from psycopg import sql

from boba.db.oracle import OraQueryBuilder, OraSql
from boba.db.oracle.payload import PayloadOracle
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.query import PgQueryBuilder
from boba.tool.ora import tools as ora
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound, RawOutbound
from boba.toolkit.stream import Chunk

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()
ROWS = 5000
CHUNK = 777

COLUMNS = "ID, EMAIL, BALANCE, CREATED_AT, NOTE, PHOTO"
PG_TABLE = sql.Identifier("ora_copy_probe")


class Sink(RawOutbound):
    """Выходной порт в память: копит всё, что записал насос."""

    def __init__(self) -> None:
        super().__init__(ToolIo.detached())
        self.chunks: list[bytes] = []

    def write(self, chunk: Chunk) -> None:
        self.chunks.append(bytes(chunk))

    def data(self) -> bytes:
        return b"".join(self.chunks)


class Feed(RawInbound):
    """Входной порт из памяти: отдаёт байты порциями произвольного размера,
    чтобы граница порции резала строки и поля CSV где попало."""

    def __init__(self, data: bytes, size: int) -> None:
        super().__init__(ToolIo.detached())
        self._data = data
        self._size = size

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._data), self._size):
            yield self._data[start : start + self._size]


async def _fingerprint(source: Any, table: str) -> Sequence[Any]:
    """Число строк, NULL и контрольные суммы таблицы Oracle."""
    payload = PayloadOracle(source.oracle)
    query = (
        OraQueryBuilder(table=OraSql(f"{DemoUser.NAME}.{table}"))
        .add(
            "select count(*), count(note), count(photo), sum(balance), "
            "min(created_at), max(created_at), sum(length(email)), "
            "sum(utl_raw.length(photo)) from {table}"
        )
        .build()
    )
    async with payload.opened() as conn, payload.rows(conn, query.text) as stream:
        rows = [row async for row in stream.blocks]

    return rows[0]


def _pg(text: str) -> sql.Composed:
    """Запрос стенда postgres с именем таблицы-пробы."""
    return PgQueryBuilder(table=PG_TABLE).add(text).build().text


@pytest.fixture(scope="module", params=[s.name for s in STAND.ora_sources])
async def target(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """Цель с пересозданной схемой TOOL_DEMO; после модуля схема сносится."""
    source = STAND.source(request.param)
    demo = ToolDemo(source)
    await demo.recreate(ROWS)
    yield source
    await demo.drop()


class TestCopyRoundTrip:
    async def test_oracle_to_postgres_to_oracle(
        self, target: Any, ix_stand: IxStand
    ) -> None:
        copy_out = ToolMain.toolset(ora.ora_copy_out)[0].coroutine
        copy_in = ToolMain.toolset(ora.ora_copy_in)[0].coroutine
        if copy_out is None or copy_in is None:
            raise AssertionError("bodies are coroutines")

        sink = Sink()
        select = (
            OraQueryBuilder(table=OraSql(f"{DemoUser.NAME}.customers"))
            .add(
                "select id, email, balance, created_at, note, "
                "rawtohex(photo) as photo from {table} order by id"
            )
            .build()
        )
        report = await copy_out(connection=target.oracle, sql=select.text, out=sink)
        assert f"copied out {len(sink.data())} bytes" == report.text
        assert sink.data().count(b"\n") == ROWS

        async with await AsyncPostgresPool.dedicated(ix_stand.ix_profile) as pg:
            await pg.execute(_pg("drop table if exists {table}"))
            await pg.execute(
                _pg(
                    "create table {table} (id bigint, email text, "
                    "balance numeric(18,2), created_at timestamp, note text, "
                    "photo bytea)"
                )
            )
            async with (
                pg.cursor() as cur,
                cur.copy(_pg("copy {table} from stdin (format csv)")) as copy,
            ):
                for chunk in sink.chunks:
                    await copy.write(chunk)

            await pg.execute(
                _pg("update {table} set photo = decode(encode(photo, 'escape'), 'hex')")
            )
            counted = await pg.execute(
                _pg("select count(*), count(note), count(photo) from {table}")
            )
            assert await counted.fetchone() == (
                ROWS,
                ROWS - ROWS // 3,
                ROWS - ROWS // 5,
            )

            exported = bytearray()
            async with (
                pg.cursor() as cur,
                cur.copy(
                    _pg(
                        "copy (select id, email, balance, created_at, note, photo "
                        "from {table} order by id) "
                        "to stdout (format csv, null '\\N')"
                    )
                ) as copy,
            ):
                async for block in copy:
                    exported.extend(block)

            await pg.execute(_pg("drop table {table}"))

        report = await copy_in(
            connection=target.demo_owner,
            table=f"{DemoUser.NAME}.SINK",
            columns=COLUMNS,
            feed=Feed(bytes(exported), CHUNK),
        )
        assert f"{ROWS} rows into {DemoUser.NAME}.SINK" in report.text

        assert await _fingerprint(target, "sink") == await _fingerprint(
            target, "customers"
        )
