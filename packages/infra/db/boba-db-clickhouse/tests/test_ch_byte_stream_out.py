"""Сырой поток ответа: байты приходят ровно такими, какими их прислал сервер в
выбранном формате, RawBLOB отдаёт значение байт в байт, а размеры блоков
задаются на запрос. Живой стенд — старейший и новейший сервер."""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.formats import RawBlob
from boba.db.clickhouse.payload import PayloadClickHouse, ReadTuning

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class StandSource(BaseModel):
    """Источник ix_stand: имя и профиль."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    clickhouse: ClickHouseConfig
    demo: bool = True


class StandSources(BaseModel):
    """Секция [ix_stand]: здесь нужен только список ch_sources."""

    model_config = ConfigDict(extra="ignore")

    ch_sources: Sequence[StandSource]


@pytest.fixture(scope="module", params=["first", "last"])
def source(request: Any, raw_config: Any) -> StandSource:
    sources = bind(raw_config, path="ix_stand", model=StandSources)
    listed = [item for item in sources.ch_sources if item.demo]
    if not listed:
        pytest.skip("ix_stand.ch_sources has no source with demo = true")

    if request.param == "first":
        return listed[0]

    return listed[-1]


class TestByteStreamOut:
    async def test_bytes_come_as_the_format_writes_them(
        self, source: StandSource
    ) -> None:
        query = "select toUInt32(number) as n, toString(number) as s from numbers(3)"
        async with (
            PayloadClickHouse.opened_config(source.clickhouse) as client,
            PayloadClickHouse.byte_stream_out(client, query, "JSONEachRow") as stream,
        ):
            data = bytearray()
            async for block in stream.blocks:
                data.extend(block)

        assert bytes(data) == (b'{"n":0,"s":"0"}\n{"n":1,"s":"1"}\n{"n":2,"s":"2"}\n')


async def _blob(connection: ClickHouseConfig, query: str) -> bytes:
    blob = RawBlob()
    async with (
        PayloadClickHouse.opened_config(connection) as client,
        PayloadClickHouse.byte_stream_out(client, query, blob.FORMAT) as raw,
    ):
        stream = await blob.read(raw.blocks)
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

    return bytes(data)


class TestRawBlob:
    async def test_value_comes_byte_for_byte(self, source: StandSource) -> None:
        payload = bytes(range(256)) * 4096

        data = await _blob(
            source.clickhouse,
            "select repeat(unhex('" + payload[:256].hex() + "'), 4096)",
        )

        assert data == payload

    async def test_rows_are_glued_without_separators(self, source: StandSource) -> None:
        data = await _blob(
            source.clickhouse,
            "select arrayJoin([toNullable('a\\t'), null, 'b\\n'])",
        )

        assert data == b"a\tb\n"


BIG = "select number, toString(number), repeat('x', 100) from numbers(300000)"


async def _sizes(
    connection: ClickHouseConfig, tuning: ReadTuning, pause: float
) -> list[int]:
    """Размеры блоков потока; pause имитирует медленного потребителя."""
    sizes: list[int] = []
    async with (
        PayloadClickHouse.opened_config(connection) as client,
        PayloadClickHouse.byte_stream_out(
            client, BIG, "TabSeparated", tuning=tuning
        ) as stream,
    ):
        async for block in stream.blocks:
            sizes.append(len(block))
            await asyncio.sleep(pause)

    return sizes


class TestReadTuning:
    """Размеры блоков задаются на запрос: первые блоки успевают прийти с
    прежними размерами, поэтому проверяется хвост потока. Чтение сокета
    отдаёт то, что уже пришло, поэтому socket_read_size — верхняя граница
    блока: 22.12 шлёт ответ кусками по 4 КиБ."""

    async def test_socket_read_size_sets_the_block(self, source: StandSource) -> None:
        tuning = ReadTuning(socket_read_size=16 * 1024)

        sizes = await _sizes(source.clickhouse, tuning, 0)

        assert max(sizes[4:]) <= 16 * 1024

    async def test_read_buffer_size_bounds_a_lagging_consumer(
        self, source: StandSource
    ) -> None:
        small = ReadTuning(socket_read_size=16 * 1024, read_buffer_size=64 * 1024)
        large = ReadTuning(socket_read_size=16 * 1024, read_buffer_size=1024 * 1024)

        bounded = await _sizes(source.clickhouse, small, 0.005)
        joined = await _sizes(source.clickhouse, large, 0.005)

        assert max(bounded[4:]) <= 2 * 64 * 1024 + 16 * 1024
        assert statistics.median(joined[4:]) > 2 * 64 * 1024 + 16 * 1024

    def test_non_positive_size_is_refused(self) -> None:
        with pytest.raises(ClickHouseQueryError, match="socket_read_size expects"):
            ReadTuning(socket_read_size=0)
