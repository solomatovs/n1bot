"""Насос ch_copy_in на живом стенде: тело TabSeparated из входного порта уезжает
одним INSERT, порции порта режут строки где попало, \\N становится NULL, имена
базы, таблицы и колонок с пробелом и обратной кавычкой квотирует драйвер."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import ChFormat, ChIdentifier, ChQueryBuilder
from boba.tool.ch import tools as ch
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


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


class Feed(RawInbound):
    """Входной порт из памяти: отдаёт байты порциями произвольного размера."""

    def __init__(self, data: bytes, size: int) -> None:
        super().__init__(ToolIo.detached())
        self._data = data
        self._size = size

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._data), self._size):
            yield self._data[start : start + self._size]


class Probe:
    """База и таблица со странными именами на источнике; после проверки сносится."""

    DATABASE: ClassVar[str] = "copy tool"
    TABLE: ClassVar[str] = "we`ird"
    COLUMNS: ClassVar[tuple[str, ...]] = ("id", "na me")
    ROWS: ClassVar[int] = 5000
    CHUNK: ClassVar[int] = 777

    def __init__(self, source: StandSource) -> None:
        self._source = source

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists %(db)s")
            await self._command(client, "create database %(db)s")
            await self._command(
                client,
                "create table %(db)s.%(t)s (%(id)s UInt64, %(note)s Nullable(String)) "
                "engine = MergeTree order by %(id)s",
                t=ChIdentifier(self.TABLE),
                id=ChIdentifier(self.COLUMNS[0]),
                note=ChIdentifier(self.COLUMNS[1]),
            )

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self._source.admin) as client:
            await self._command(client, "drop database if exists %(db)s")

    def tsv(self) -> bytes:
        lines: list[bytes] = []
        for number in range(self.ROWS):
            if number % 3 == 0:
                lines.append(f"{number}\t\\N\n".encode())
                continue

            lines.append(f"{number}\ttab\\there {number}\n".encode())

        return b"".join(lines)

    async def summary(self) -> Sequence[Any]:
        query = (
            ChQueryBuilder()
            .add(
                "select count(), count(%(note)s), max(%(note)s), sum(%(id)s) "
                "from %(db)s.%(t)s",
                note=ChIdentifier(self.COLUMNS[1]),
                id=ChIdentifier(self.COLUMNS[0]),
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
async def probe(source: StandSource) -> AsyncIterator[Probe]:
    made = Probe(source)
    await made.recreate()
    yield made
    await made.drop()


class TestCopyIn:
    async def test_tsv_feed_lands_in_the_table(
        self, probe: Probe, source: StandSource
    ) -> None:
        copy_in = ToolMain.toolset(ch.ch_copy_in)[0].coroutine
        if copy_in is None:
            raise AssertionError("body is a coroutine")

        data = probe.tsv()
        report = await copy_in(
            connection=source.admin,
            database=Probe.DATABASE,
            table=Probe.TABLE,
            columns=list(Probe.COLUMNS),
            fmt=ChFormat.TSV,
            feed=Feed(data, Probe.CHUNK),
        )

        assert report.text == (
            f"copied in {len(data)} bytes, {Probe.ROWS} rows "
            f"into {Probe.DATABASE}.{Probe.TABLE}"
        )
        assert await probe.summary() == (
            Probe.ROWS,
            Probe.ROWS - (Probe.ROWS + 2) // 3,
            "tab\there 998",
            Probe.ROWS * (Probe.ROWS - 1) // 2,
        )
