"""Запрос с потоковым телом и клиентский режим драйвера на живом ClickHouse
стенда: INSERT ... FORMAT CSV собран билдером, тело уезжает блоками с
произвольными границами, имена базы, таблицы и колонок с обратной кавычкой и
пробелом квотирует драйвер, пустое поле CSV становится NULL, а выборка через
%(name)s и ChIdentifier читает то же обратно; серверные параметры с телом
отвергаются."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import (
    ChIdentifier,
    ChIdentifiers,
    ChQueryBuilder,
    ChValue,
)

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


class Probe:
    """База со странными именами на источнике: имена квотирует драйвер в
    клиентском режиме, после проверки база сносится."""

    DATABASE: ClassVar[str] = "copy_stand"
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

    def csv(self) -> bytes:
        lines: list[bytes] = []
        for number in range(self.ROWS):
            if number % 3 == 0:
                lines.append(f"{number},\n".encode())
                continue

            lines.append(f'{number},"o\'neil {number}"\n'.encode())

        return b"".join(lines)

    async def blocks(self, data: bytes) -> AsyncIterator[bytes]:
        for start in range(0, len(data), self.CHUNK):
            yield data[start : start + self.CHUNK]

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


class TestStreamIn:
    async def test_csv_blocks_land_in_one_insert(
        self, probe: Probe, source: StandSource
    ) -> None:
        data = probe.csv()
        async with PayloadClickHouse.opened_config(source.admin) as client:
            insert = (
                ChQueryBuilder()
                .add(
                    "insert into %(db)s.%(t)s (%(columns)s) format $fmt",
                    db=ChIdentifier(Probe.DATABASE),
                    t=ChIdentifier(Probe.TABLE),
                    columns=ChIdentifiers(Probe.COLUMNS),
                    fmt="CSV",
                )
                .build()
            )
            summary = await PayloadClickHouse.byte_stream_in(
                client, insert.text, insert.params, blocks=probe.blocks(data)
            )

            query = (
                ChQueryBuilder()
                .add(
                    "select count(), count(%(text)s), max(%(text)s) from %(db)s.%(t)s",
                    text=ChIdentifier("na me"),
                    db=ChIdentifier(Probe.DATABASE),
                    t=ChIdentifier(Probe.TABLE),
                )
                .add(
                    "where %(text)s is null or %(text)s like %(mask)s",
                    mask=ChValue("o'neil %"),
                )
                .build()
            )
            async with PayloadClickHouse.rows_stream_out(
                client, query.text, query.params
            ) as stream:
                rows = [row async for row in stream.blocks]

            listed = (
                ChQueryBuilder()
                .add(
                    "select %(columns)s from %(db)s.%(t)s order by id limit 2",
                    columns=ChIdentifiers(Probe.COLUMNS),
                    db=ChIdentifier(Probe.DATABASE),
                    t=ChIdentifier(Probe.TABLE),
                )
                .build()
            )
            async with PayloadClickHouse.rows_stream_out(
                client, listed.text, listed.params
            ) as stream:
                head = [tuple(row) async for row in stream.blocks]

        assert summary.written_rows == Probe.ROWS
        assert rows == [(Probe.ROWS, Probe.ROWS - (Probe.ROWS + 2) // 3, "o'neil 998")]
        assert head == [(0, None), (1, "o'neil 1")]

    async def test_server_parameters_are_refused_with_a_body(
        self, probe: Probe, source: StandSource
    ) -> None:
        insert = (
            ChQueryBuilder()
            .add(
                "insert into {db:Identifier}.{t:Identifier} format CSV",
                db=ChValue("x"),
                t=ChValue("y"),
            )
            .build()
        )
        async with PayloadClickHouse.opened_config(source.admin) as client:
            with pytest.raises(ClickHouseQueryError, match="server parameters"):
                await PayloadClickHouse.byte_stream_in(
                    client, insert.text, insert.params, blocks=probe.blocks(b"")
                )

    async def test_byte_stream_pipes_into_stream_in(
        self, probe: Probe, source: StandSource
    ) -> None:
        """Блоки memoryview сырого потока одного запроса уходят телом вставки
        другого без копий и разбора на Python."""
        rows = 100000
        create = (
            ChQueryBuilder()
            .add(
                "create table %(db)s.%(t)s (n UInt64, s String) engine = Memory",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("pipe"),
            )
            .build()
        )
        select = (
            ChQueryBuilder()
            .add(
                "select number, toString(number) from numbers(%(rows)s)",
                rows=ChValue(rows),
            )
            .build()
        )
        insert = (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s format $fmt",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("pipe"),
                fmt="TabSeparated",
            )
            .build()
        )
        count = (
            ChQueryBuilder()
            .add(
                "select count(), sum(n), sum(length(s)) from %(db)s.%(t)s",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("pipe"),
            )
            .build()
        )

        async with PayloadClickHouse.opened_config(source.admin) as client:
            await client.command(create.text, parameters=create.params)
            async with PayloadClickHouse.byte_stream_out(
                client, select.text, "TabSeparated", select.params
            ) as stream:
                summary = await PayloadClickHouse.byte_stream_in(
                    client, insert.text, insert.params, blocks=stream.blocks
                )

            async with PayloadClickHouse.rows_stream_out(
                client, count.text, count.params
            ) as counted:
                totals = [tuple(row) async for row in counted.blocks]

        lengths = 0
        for number in range(rows):
            lengths += len(str(number))

        assert summary.written_rows == rows
        assert totals == [(rows, rows * (rows - 1) // 2, lengths)]
