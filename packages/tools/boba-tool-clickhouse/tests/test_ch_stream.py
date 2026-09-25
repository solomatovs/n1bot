"""Насосы ch_stream_out и ch_stream_in на живых стендах: стейтмент уходит
серверу как написан, формат задаёт текст запроса, байты идут между узлами
без разбора; цепочка ClickHouse -> ClickHouse сопоставляет колонки по именам.
Цепочки с другими базами живут в boba-pump-stand."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChQueryBuilder
from boba.tool.ch import tools as ch
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound, RawOutbound
from boba.toolkit.stream import Chunk

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROWS = 5000
CHUNK = 777


class StandSource(BaseModel):
    """Источник ix_stand: имя и профиль; demo — можно ли на нём создавать базы."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    @property
    def admin(self) -> ClickHouseConfig:
        return self.clickhouse.model_copy(
            update={"settings": ClickHouseSettingsConfig.model_validate({})}
        )


class StandSources(BaseModel):
    """Секция [ix_stand]: здесь нужен только список ch_sources."""

    model_config = ConfigDict(extra="ignore")

    ch_sources: Sequence[StandSource]


class Sink(RawOutbound):
    """Выходной порт в память: копит всё, что записал насос."""

    def __init__(self) -> None:
        super().__init__(ToolIo.detached())
        self._buffer = bytearray()

    async def write(self, chunk: Chunk) -> None:
        self._buffer.extend(chunk)

    def data(self) -> bytes:
        return bytes(self._buffer)


class Feed(RawInbound):
    """Входной порт из памяти: порции своего размера режут строки где попало,
    chunk_bytes насоса не смотрит."""

    def __init__(self, data: bytes, size: int) -> None:
        super().__init__(ToolIo.detached())
        self._data = data
        self._size = size

    def read(self, chunk_bytes: int) -> Iterator[bytes]:
        for start in range(0, len(self._data), self._size):
            yield self._data[start : start + self._size]


class Stand:
    """База стенда насосов на источнике: таблица customers с NULL, пустая
    таблица-приёмник с колонками в другом порядке; после модуля сносится."""

    DATABASE: ClassVar[str] = "stream_stand"

    def __init__(self, source: StandSource) -> None:
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


def _ch(text: str, **bind: str) -> str:
    """Стейтмент для насоса: имена стенда подставляются голым текстом."""
    return ChQueryBuilder().add(text, db=Stand.DATABASE, **bind).build().text


@pytest.fixture(scope="module", params=["first", "last"])
def source(request: Any, raw_config: Any) -> StandSource:
    """Самый старый и самый новый сервер стенда."""
    sources = bind(raw_config, path="ix_stand", model=StandSources)
    listed = [item for item in sources.ch_sources if item.demo]
    if not listed:
        pytest.skip("ix_stand.ch_sources has no source with demo = true")

    if request.param == "first":
        return listed[0]

    return listed[-1]


@pytest.fixture(scope="module")
async def stand(source: StandSource) -> AsyncIterator[Stand]:
    made = Stand(source)
    await made.recreate()
    yield made
    await made.drop()


class Pumps:
    """Тела насосов, вызванные напрямую с профилем соединения стенда."""

    def __init__(self, connection: ClickHouseConfig) -> None:
        self._connection = connection
        stream_out = ToolMain.toolset(ch.ch_stream_out)[0].coroutine
        stream_in = ToolMain.toolset(ch.ch_stream_in)[0].coroutine
        if stream_out is None or stream_in is None:
            raise AssertionError("bodies are coroutines")

        self._out = stream_out
        self._in = stream_in

    async def out(self, statement: str) -> Sink:
        sink = Sink()
        report = await self._out(
            connection=self._connection, sql=statement, chunk_bytes=CHUNK, out=sink
        )
        if report.text != "stream completed":
            raise AssertionError(report.text)

        return sink

    async def into(self, statement: str, data: bytes) -> str:
        report = await self._in(
            connection=self._connection,
            sql=statement,
            chunk_bytes=CHUNK,
            feed=Feed(data, CHUNK),
        )

        return report.text


class TestClickHouseToClickHouse:
    async def test_names_in_the_header_map_the_columns(
        self, stand: Stand, source: StandSource
    ) -> None:
        pumps = Pumps(source.admin)
        await stand.truncate("sink")

        sink = await pumps.out(
            _ch(
                "select * from $db.customers order by id "
                "format TabSeparatedWithNamesAndTypes"
            )
        )
        report = await pumps.into(
            _ch("insert into $db.sink format TabSeparatedWithNamesAndTypes"),
            sink.data(),
        )

        assert sink.data().count(b"\n") == ROWS + 2
        assert report == f"{ROWS} rows written"
        assert await stand.fingerprint("sink") == await stand.fingerprint("customers")

    async def test_server_error_reaches_the_caller(
        self, stand: Stand, source: StandSource
    ) -> None:
        pumps = Pumps(source.admin)

        with pytest.raises(ch.ClickHouseQueryError, match=r"UNKNOWN_TABLE"):
            await pumps.into(
                _ch("insert into $db.no_such_table format TabSeparated"),
                b"1\tx\n",
            )
