"""Сырой поток ответа в выбранном формате и поток TabSeparatedWithNamesAndTypes поверх
него. Сырой поток отдаёт байты ровно такими, какими их прислал сервер. Поток
TabSeparatedWithNamesAndTypes: имена колонок снимаются с первой строки ответа,
в том числе у пустого результата и с экранированными символами в именах, а
данные после шапки доходят байт в байт. Имя из всех символов ASCII, пробелов и
иероглифов возвращается ровно таким, каким его задал запрос. Живой стенд —
старейший и новейший сервер."""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.payload import PayloadClickHouse, ReadTuning
from boba.db.clickhouse.query import ChIdentifier, ChQueryBuilder

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EVERY_CHAR = "".join(map(chr, range(128))) + " 中文 名字 "
TYPED = (
    "select cast(1, 'Enum8(\\'a\\tb\\' = 1)') as e, "
    "cast(null, 'Nullable(String)') as ns, "
    "cast([(1, 'x')], 'Array(Tuple(UInt8, String))') as at, "
    "toDateTime64('2024-01-02 03:04:05', 3, 'UTC') as dt, "
    "cast(map('k', 1), 'Map(String, UInt32)') as m, "
    "toLowCardinality('x') as lc"
)
WEIRD_NAMES = ("we\\ird", "ta\tb", "new\nline", "к'в", "ba`ck", EVERY_CHAR, "\x00\x01")


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


async def _read(
    connection: ClickHouseConfig,
    query: str,
    parameters: Mapping[str, Any] | None = None,
) -> tuple[Sequence[str], Sequence[str], bytes]:
    async with (
        PayloadClickHouse.opened_config(connection) as client,
        PayloadClickHouse.tsv_stream_out(client, query, parameters) as stream,
    ):
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

    types: list[str] = []
    for column_type in stream.column_types:
        types.append(column_type.name)

    return stream.names, types, bytes(data)


class TestTsvStreamOut:
    async def test_names_of_an_empty_result(self, source: StandSource) -> None:
        names, types, data = await _read(
            source.clickhouse, "select 1 as a, 'x' as b where 0"
        )

        assert names == ("a", "b")
        assert types == ["UInt8", "String"]
        assert data == b""

    async def test_escaped_names_are_restored(self, source: StandSource) -> None:
        builder = ChQueryBuilder().add(
            "select 0 as %(n0)s", n0=ChIdentifier(WEIRD_NAMES[0])
        )
        for position in range(1, len(WEIRD_NAMES)):
            key = f"n{position}"
            name = ChIdentifier(WEIRD_NAMES[position])
            builder.add(f", {position} as %({key})s", **{key: name})

        query = builder.build()
        names, _, data = await _read(source.clickhouse, query.text, query.params)

        assert names == WEIRD_NAMES
        assert len(data.split(b"\t")) == len(WEIRD_NAMES)

    async def test_rows_after_the_header_pass_as_is(self, source: StandSource) -> None:
        names, types, data = await _read(
            source.clickhouse,
            "select number as n, toString(number) as s from numbers(100000)",
        )

        expected = bytearray()
        for number in range(100000):
            expected.extend(f"{number}\t{number}\n".encode())

        assert names == ("n", "s")
        assert types == ["UInt64", "String"]
        assert data == bytes(expected)

    async def test_types_match_the_native_protocol(self, source: StandSource) -> None:
        names, types, _ = await _read(source.clickhouse, TYPED)

        async with (
            PayloadClickHouse.opened_config(source.clickhouse) as client,
            PayloadClickHouse.rows_stream_out(client, TYPED) as stream,
        ):
            native: list[str] = []
            for column_type in stream.column_types:
                native.append(column_type.name)

        assert names == ("e", "ns", "at", "dt", "m", "lc")
        assert types == native
        assert types == [
            "Enum8('a\\tb' = 1)",
            "Nullable(String)",
            "Array(Tuple(UInt8, String))",
            "DateTime64(3, 'UTC')",
            "Map(String, UInt32)",
            "LowCardinality(String)",
        ]


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
    async with (
        PayloadClickHouse.opened_config(connection) as client,
        PayloadClickHouse.blob_stream_out(client, query) as stream,
    ):
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

    return bytes(data)


class TestBlobStreamOut:
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


async def _read_json(
    connection: ClickHouseConfig,
    query: str,
    parameters: Mapping[str, Any] | None = None,
) -> tuple[Sequence[str], Sequence[str], bytes]:
    async with (
        PayloadClickHouse.opened_config(connection) as client,
        PayloadClickHouse.json_compact_stream_out(client, query, parameters) as stream,
    ):
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

    types: list[str] = []
    for column_type in stream.column_types:
        types.append(column_type.name)

    return stream.names, types, bytes(data)


class TestJsonCompactStreamOut:
    async def test_names_and_types_come_from_the_json_header(
        self, source: StandSource
    ) -> None:
        builder = ChQueryBuilder().add(
            "select 0 as %(n0)s", n0=ChIdentifier(WEIRD_NAMES[0])
        )
        for position in range(1, len(WEIRD_NAMES)):
            key = f"n{position}"
            name = ChIdentifier(WEIRD_NAMES[position])
            builder.add(f", {position} as %({key})s", **{key: name})

        query = builder.build()
        names, types, data = await _read_json(
            source.clickhouse, query.text, query.params
        )

        assert names == WEIRD_NAMES
        assert types == ["UInt8"] * len(WEIRD_NAMES)
        assert data == b"[0, 1, 2, 3, 4, 5, 6]\n"

    async def test_types_match_the_native_protocol(self, source: StandSource) -> None:
        _, json_types, _ = await _read_json(source.clickhouse, TYPED)
        _, tsv_types, _ = await _read(source.clickhouse, TYPED)

        assert json_types == tsv_types

    async def test_empty_result_still_has_a_header(self, source: StandSource) -> None:
        names, types, data = await _read_json(
            source.clickhouse, "select 1 as a, 'x' as b where 0"
        )

        assert names == ("a", "b")
        assert types == ["UInt8", "String"]
        assert data == b""


EXACT = (
    "select toFloat64('nan') as nan, toFloat64('-inf') as ninf, "
    "18446744073709551615 as u64, toFloat64('1e308') as f64, "
    "toDecimal64('-12345.6789', 4) as dec, unhex('ff00') as bin, 'a/b' as slash"
)


@pytest.fixture(scope="module")
def oldest_and_newest(raw_config: Any) -> tuple[StandSource, StandSource]:
    sources = bind(raw_config, path="ix_stand", model=StandSources)
    listed = [item for item in sources.ch_sources if item.demo]
    if len(listed) < 2:
        pytest.skip("ix_stand.ch_sources needs two sources with demo = true")

    return listed[0], listed[-1]


async def test_json_bytes_are_the_same_across_versions(
    oldest_and_newest: tuple[StandSource, StandSource],
) -> None:
    oldest, newest = oldest_and_newest

    _, _, old = await _read_json(oldest.clickhouse, EXACT)
    _, _, new = await _read_json(newest.clickhouse, EXACT)

    assert old == new
    assert old == (
        b'["nan", "-inf", "18446744073709551615", "1e308", "-12345.6789", '
        b'"\xff\\u0000", "a\\/b"]\n'
    )
