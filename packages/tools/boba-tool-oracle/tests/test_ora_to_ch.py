"""Цепочка Oracle -> ClickHouse на живых стендах: CSV из ora_copy_out уезжает
в ch_copy_in как есть, без разбора между узлами; число строк, NULL и суммы
совпадают на обеих сторонах."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from ora_tool_stand import DemoUser, IxStand, ToolDemo
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChFormat, ChIdentifier, ChQueryBuilder
from boba.db.oracle import OraIdentifier, OraQueryBuilder
from boba.db.oracle.payload import PayloadOracle
from boba.tool.ch import tools as ch
from boba.tool.ora import tools as ora
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound, RawOutbound
from boba.toolkit.stream import Chunk

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = IxStand.required()
ROWS = 5000
CHUNK = 777

CUSTOMERS = OraIdentifier(f"{DemoUser.NAME}.customers")
SELECT = (
    OraQueryBuilder(table=CUSTOMERS)
    .add(
        "select id, email, balance, created_at, note, rawtohex(photo) as photo "
        "from {table} order by id"
    )
    .build()
)
FINGERPRINT = (
    OraQueryBuilder(table=CUSTOMERS)
    .add(
        "select count(*), count(note), count(photo), sum(balance), "
        "sum(length(email)), sum(length(rawtohex(photo))) from {table}"
    )
    .build()
)


class ChSource(BaseModel):
    """Источник ClickHouse стенда: профиль и разрешение создавать базы."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True

    @property
    def admin(self) -> ClickHouseConfig:
        return self.clickhouse.model_copy(
            update={"settings": ClickHouseSettingsConfig.model_validate({})}
        )


class ChSources(BaseModel):
    """Секция [ix_stand]: здесь нужен только список ch_sources."""

    model_config = ConfigDict(extra="ignore")

    ch_sources: Sequence[ChSource]


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
    """Входной порт из памяти: порции произвольного размера."""

    def __init__(self, data: bytes, size: int) -> None:
        super().__init__(ToolIo.detached())
        self._data = data
        self._size = size

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._data), self._size):
            yield self._data[start : start + self._size]


class ChTarget:
    """База-приёмник на ClickHouse с таблицей под колонки customers."""

    DATABASE: ClassVar[str] = "ora_copy_stand"
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
        self._source = source

    def admin_connection(self) -> ClickHouseConfig:
        return self._source.admin

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
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
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
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
            PayloadClickHouse.opened_config(self._source.admin) as client,
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


async def _ora_fingerprint(source: Any) -> tuple[Any, ...]:
    payload = PayloadOracle(source.oracle)
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
async def oracle() -> AsyncIterator[Any]:
    """Первый источник Oracle с пересозданной схемой TOOL_DEMO."""
    source = STAND.ora_sources[0]
    demo = ToolDemo(source)
    await demo.recreate(ROWS)
    yield source
    await demo.drop()


@pytest.fixture(scope="module")
async def clickhouse(raw_config: Any) -> AsyncIterator[ChTarget]:
    """Самый новый demo-источник ClickHouse с базой-приёмником."""
    sources = bind(raw_config, path="ix_stand", model=ChSources)
    listed = [item for item in sources.ch_sources if item.demo]
    if not listed:
        pytest.skip("ix_stand.ch_sources has no source with demo = true")

    target = ChTarget(listed[-1])
    await target.recreate()
    yield target
    await target.drop()


class TestOracleToClickHouse:
    async def test_csv_goes_through_untouched(
        self, oracle: Any, clickhouse: ChTarget
    ) -> None:
        copy_out = ToolMain.toolset(ora.ora_copy_out)[0].coroutine
        copy_in = ToolMain.toolset(ch.ch_copy_in)[0].coroutine
        if copy_out is None or copy_in is None:
            raise AssertionError("bodies are coroutines")

        sink = Sink()
        await copy_out(connection=oracle.oracle, sql=SELECT.text, out=sink)
        assert sink.data().count(b"\n") == ROWS

        report = await copy_in(
            connection=clickhouse.admin_connection(),
            database=ChTarget.DATABASE,
            table=ChTarget.TABLE,
            columns=list(ChTarget.COLUMNS),
            fmt=ChFormat.CSV,
            feed=Feed(sink.data(), CHUNK),
        )
        assert f"{ROWS} rows into {ChTarget.DATABASE}.{ChTarget.TABLE}" in report.text

        assert _normalized(await clickhouse.fingerprint()) == _normalized(
            await _ora_fingerprint(oracle)
        )
