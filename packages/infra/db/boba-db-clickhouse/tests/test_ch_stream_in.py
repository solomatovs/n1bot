"""Запрос с потоковым телом и клиентский режим драйвера на живом ClickHouse
стенда: INSERT ... FORMAT CSV собран билдером, тело уезжает блоками с
произвольными границами, имена базы, таблицы и колонок с обратной кавычкой и
пробелом квотирует драйвер, пустое поле CSV становится NULL, а выборка через
%(name)s и ChIdentifier читает то же обратно; серверные параметры с телом
отвергаются."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.errors import ClickHouseQueryError
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import (
    ChFormat,
    ChIdentifier,
    ChIdentifiers,
    ChQuery,
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
                    fmt=ChFormat.CSV.value,
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
                fmt=ChFormat.TSV.value,
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
                client, select.text, ChFormat.TSV.value, select.params
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


PIPE_NAMES = (
    "id",
    "na me",
    "ta\tb",
    "new\nline",
    "к'в 中文",
    "ba`ck",
    "we\\ird",
    "".join(map(chr, range(1, 128))),
)


class TestTsvStreamIn:
    """Выход tsv_stream_out подаётся на вход tsv_stream_in как есть: шапку
    пишет payload, сервер сопоставляет колонки по именам."""

    async def test_columns_land_by_name(
        self, probe: Probe, source: StandSource
    ) -> None:
        reversed_names = list(reversed(PIPE_NAMES))
        create = ChQueryBuilder().add(
            "create table %(db)s.%(t)s (%(c0)s UInt8",
            db=ChIdentifier(Probe.DATABASE),
            t=ChIdentifier("tsv in"),
            c0=ChIdentifier(reversed_names[0]),
        )
        for position in range(1, len(reversed_names)):
            key = f"c{position}"
            create.add(
                f", %({key})s UInt8", **{key: ChIdentifier(reversed_names[position])}
            )

        create.add(") engine = Memory")

        select = ChQueryBuilder().add(
            "select 0 as %(n0)s", n0=ChIdentifier(PIPE_NAMES[0])
        )
        for position in range(1, len(PIPE_NAMES)):
            key = f"n{position}"
            select.add(
                f", {position} as %({key})s",
                **{key: ChIdentifier(PIPE_NAMES[position])},
            )

        select.add("from numbers(3)")

        insert = (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("tsv in"),
            )
            .build()
        )
        read = (
            ChQueryBuilder()
            .add(
                "select %(columns)s from %(db)s.%(t)s",
                columns=ChIdentifiers(PIPE_NAMES),
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("tsv in"),
            )
            .build()
        )

        created = create.build()
        selected = select.build()
        async with PayloadClickHouse.opened_config(source.admin) as client:
            await client.command(created.text, parameters=created.params)
            async with PayloadClickHouse.tsv_stream_out(
                client, selected.text, selected.params
            ) as stream:
                summary = await PayloadClickHouse.tsv_stream_in(
                    client, insert.text, insert.params, stream=stream
                )

            async with PayloadClickHouse.rows_stream_out(
                client, read.text, read.params
            ) as landed:
                rows = [tuple(row) async for row in landed.blocks]

        expected = tuple(range(len(PIPE_NAMES)))

        assert summary.written_rows == 3
        assert rows == [expected, expected, expected]

    async def test_type_mismatch_is_refused(
        self, probe: Probe, source: StandSource
    ) -> None:
        create = (
            ChQueryBuilder()
            .add(
                "create table %(db)s.%(t)s (n UInt64) engine = Memory",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("tsv typed"),
            )
            .build()
        )
        insert = (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("tsv typed"),
            )
            .build()
        )

        async with PayloadClickHouse.opened_config(source.admin) as client:
            await client.command(create.text, parameters=create.params)
            async with PayloadClickHouse.tsv_stream_out(
                client, "select toUInt32(1) as n"
            ) as stream:
                with pytest.raises(ClickHouseQueryError, match="must be UInt64"):
                    await PayloadClickHouse.tsv_stream_in(
                        client, insert.text, insert.params, stream=stream
                    )


class JsonTable:
    """Таблица-приёмник JSON-вставок в базе пробы: пересоздаётся на тест."""

    def __init__(self, name: str, columns: str) -> None:
        self._name = name
        self._columns = columns

    def insert(self) -> ChQuery:
        return (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier(self._name),
            )
            .build()
        )

    async def recreate(self, client: Any) -> None:
        drop = (
            ChQueryBuilder()
            .add(
                "drop table if exists %(db)s.%(t)s",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier(self._name),
            )
            .build()
        )
        create = (
            ChQueryBuilder()
            .add(
                "create table %(db)s.%(t)s ($columns) engine = Memory",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier(self._name),
                columns=self._columns,
            )
            .build()
        )
        await client.command(drop.text, parameters=drop.params)
        await client.command(create.text, parameters=create.params)

    async def rows(self, client: Any, select: str) -> list[tuple[Any, ...]]:
        query = (
            ChQueryBuilder()
            .add(
                "select $select from %(db)s.%(t)s order by 1",
                select=select,
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier(self._name),
            )
            .build()
        )
        async with PayloadClickHouse.rows_stream_out(
            client, query.text, query.params
        ) as stream:
            return [tuple(row) async for row in stream.blocks]


class TestJsonCompactStreamIn:
    async def test_columns_land_by_name(
        self, probe: Probe, source: StandSource
    ) -> None:
        reversed_names = list(reversed(PIPE_NAMES))
        create = ChQueryBuilder().add(
            "create table %(db)s.%(t)s (%(c0)s UInt8",
            db=ChIdentifier(Probe.DATABASE),
            t=ChIdentifier("json in"),
            c0=ChIdentifier(reversed_names[0]),
        )
        for position in range(1, len(reversed_names)):
            key = f"c{position}"
            create.add(
                f", %({key})s UInt8", **{key: ChIdentifier(reversed_names[position])}
            )

        create.add(") engine = Memory")

        select = ChQueryBuilder().add(
            "select 0 as %(n0)s", n0=ChIdentifier(PIPE_NAMES[0])
        )
        for position in range(1, len(PIPE_NAMES)):
            key = f"n{position}"
            select.add(
                f", {position} as %({key})s",
                **{key: ChIdentifier(PIPE_NAMES[position])},
            )

        select.add("from numbers(3)")

        table = JsonTable("json in", "")
        read = (
            ChQueryBuilder()
            .add(
                "select %(columns)s from %(db)s.%(t)s",
                columns=ChIdentifiers(PIPE_NAMES),
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("json in"),
            )
            .build()
        )

        created = create.build()
        selected = select.build()
        insert = table.insert()
        async with PayloadClickHouse.opened_config(source.admin) as client:
            await client.command(created.text, parameters=created.params)
            async with PayloadClickHouse.json_compact_stream_out(
                client, selected.text, selected.params
            ) as stream:
                summary = await PayloadClickHouse.json_compact_stream_in(
                    client, insert.text, insert.params, stream=stream
                )

            async with PayloadClickHouse.rows_stream_out(
                client, read.text, read.params
            ) as landed:
                rows = [tuple(row) async for row in landed.blocks]

        expected = tuple(range(len(PIPE_NAMES)))

        assert summary.written_rows == 3
        assert rows == [expected, expected, expected]


RECORDS = [{"n": 1, "s": "a\tb"}, {"s": "ё 中文", "n": 2, "extra": {"x": [1, 2]}}]


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), 7):
        yield data[start : start + 7]


class TestJsonlStreamIn:
    @pytest.mark.parametrize(
        "document",
        [
            "\n".join(json.dumps(record, ensure_ascii=False) for record in RECORDS),
            json.dumps(RECORDS),
            json.dumps(RECORDS, indent=2, ensure_ascii=False),
        ],
        ids=["lines", "array", "pretty array"],
    )
    async def test_records_land_by_key(
        self, probe: Probe, source: StandSource, document: str
    ) -> None:
        table = JsonTable("jsonl", "n UInt64, s String")
        insert = table.insert()
        async with PayloadClickHouse.opened_config(source.admin) as client:
            await table.recreate(client)
            summary = await PayloadClickHouse.jsonl_stream_in(
                client, insert.text, insert.params, blocks=_chunks(document.encode())
            )
            rows = await table.rows(client, "n, s")

        assert summary.written_rows == 2
        assert rows == [(1, "a\tb"), (2, "ё 中文")]

    async def test_wrapped_records_are_refused_in_strict_mode(
        self, probe: Probe, source: StandSource
    ) -> None:
        table = JsonTable("jsonl strict", "n UInt64, s String")
        insert = table.insert()
        document = json.dumps({"items": RECORDS}).encode()
        async with PayloadClickHouse.opened_config(source.admin) as client:
            await table.recreate(client)
            with pytest.raises(ClickHouseQueryError, match="items"):
                await PayloadClickHouse.jsonl_stream_in(
                    client,
                    insert.text,
                    insert.params,
                    settings={"input_format_skip_unknown_fields": 0},
                    blocks=_chunks(document),
                )


class TestJsonDocumentStreamIn:
    async def test_each_document_is_one_row(
        self, probe: Probe, source: StandSource
    ) -> None:
        table = JsonTable("json doc", "doc String")
        insert = table.insert()
        first = json.dumps({"items": RECORDS, "meta": {"k": "v1"}}, indent=2)
        second = json.dumps({"items": RECORDS[:1], "meta": {"k": "v2"}})
        document = (first + "\n" + second).encode()
        async with PayloadClickHouse.opened_config(source.admin) as client:
            await table.recreate(client)
            summary = await PayloadClickHouse.json_document_stream_in(
                client, insert.text, insert.params, blocks=_chunks(document)
            )
            rows = await table.rows(
                client,
                "JSONExtractString(doc, 'meta', 'k'), "
                "length(JSONExtractArrayRaw(doc, 'items'))",
            )

        assert summary.written_rows == 2
        assert rows == [("v1", 2), ("v2", 1)]


HARD_COLUMNS = (
    "id UInt64, f64 Float64, f32 Float32, u64 UInt64, i128 Int128, u256 UInt256, "
    "dec Decimal(38, 10), bin String, fixed FixedString(3), "
    "dt DateTime64(9, 'UTC'), "
    "nested Tuple(a Float64, b Array(Nullable(Float64))), m Map(String, Float64)"
)
HARD_VALUES = (
    "select rowNumberInAllBlocks(), * from (select "
    "arrayJoin([toFloat64('nan'), toFloat64('inf'), toFloat64('-inf'), "
    "toFloat64('-0'), 0.1 + 0.2, toFloat64('1e308'), toFloat64('5e-324')]), "
    "toFloat32(arrayJoin([toFloat64('nan'), toFloat64('-inf'), 0.1])), "
    "arrayJoin([18446744073709551615, 0]), "
    "toInt128('-170141183460469231731687303715884105728'), "
    "toUInt256('115792089237316195423570985008687907853269984665640564039457584"
    "007913129639935'), "
    "toDecimal128('-1234567890123456789012345678.0123456789', 10), "
    "arrayJoin([unhex('ff00fe'), 'a/b\\\\c\"d', '']), "
    "toFixedString(unhex('00ff41'), 3), "
    "toDateTime64('2262-04-11 23:47:16.854775807', 9, 'UTC'), "
    "tuple(toFloat64('nan'), [toFloat64('inf'), null, toFloat64('-inf')]), "
    "map('k', toFloat64('nan')))"
)


async def _tsv_dump(client: Any, table: str) -> bytes:
    query = (
        ChQueryBuilder()
        .add(
            "select * from %(db)s.%(t)s order by id",
            db=ChIdentifier(Probe.DATABASE),
            t=ChIdentifier(table),
        )
        .build()
    )
    data = bytearray()
    async with PayloadClickHouse.byte_stream_out(
        client, query.text, ChFormat.TSV.value, query.params
    ) as stream:
        async for block in stream.blocks:
            data.extend(block)

    return bytes(data)


class TestJsonExactRoundTrip:
    """NaN, Inf, -0, 64-битные и 128/256-битные целые, Decimal, бинарные
    строки, FixedString, наносекунды и вложенные NaN проходят через
    json_compact_stream_out и json_compact_stream_in без потерь: таблица после
    перекачки совпадает с исходной в TSV байт в байт."""

    async def test_hard_values_survive_json(
        self, probe: Probe, source: StandSource
    ) -> None:
        origin = JsonTable("exact src", HARD_COLUMNS)
        target = JsonTable("exact dst", HARD_COLUMNS)
        fill = (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s $values",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("exact src"),
                values=HARD_VALUES,
            )
            .build()
        )
        read = (
            ChQueryBuilder()
            .add(
                "select * from %(db)s.%(t)s order by id",
                db=ChIdentifier(Probe.DATABASE),
                t=ChIdentifier("exact src"),
            )
            .build()
        )
        insert = target.insert()

        async with PayloadClickHouse.opened_config(source.admin) as client:
            await origin.recreate(client)
            await target.recreate(client)
            await client.command(fill.text, parameters=fill.params)
            async with PayloadClickHouse.json_compact_stream_out(
                client, read.text, read.params
            ) as stream:
                await PayloadClickHouse.json_compact_stream_in(
                    client, insert.text, insert.params, stream=stream
                )

            before = await _tsv_dump(client, "exact src")
            after = await _tsv_dump(client, "exact dst")

        assert before.count(b"\n") == 7 * 3 * 2 * 3
        assert after == before
