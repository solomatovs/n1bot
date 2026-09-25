"""Форматеры потоков поверх сырых байтовых потоков payload. С шапкой
(TabSeparatedWithNamesAndTypes, JSONCompactEachRowWithNamesAndTypes,
CSVWithNamesAndTypes; CSVWithNames — только имена): имена и
типы снимаются с первых двух строк, в том числе у пустого результата и с
любыми символами в именах, данные после шапки доходят байт в байт, обратный
путь через write сопоставляет колонки по именам, а трудные значения проходят
без потерь. JSONEachRow принимает JSON Lines, массив и объект, JSONAsString —
документ целиком. Живой стенд — старейший и новейший сервер; разбор шапки
без стенда — на потоках из памяти."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from boba.config import bind
from boba.db.clickhouse.connection import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.clickhouse.errors import ClickHouseFormatError, ClickHouseQueryError
from boba.db.clickhouse.formats import (
    Csv,
    CsvWithNames,
    CsvWithNamesAndTypes,
    CustomSeparated,
    CustomSeparatedSpec,
    CustomSeparatedWithNames,
    CustomSeparatedWithNamesAndTypes,
    EscapingRule,
    JsonCompactWithNamesAndTypes,
    JsonDocuments,
    JsonLines,
    StreamFormat,
    TsvWithNamesAndTypes,
)
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.query import (
    ChIdentifier,
    ChIdentifiers,
    ChQuery,
    ChQueryBuilder,
)

pytestmark = [pytest.mark.anyio]

EVERY_CHAR = "".join(map(chr, range(128))) + " 中文 名字 "
WEIRD_NAMES = ("we\\ird", "ta\tb", "new\nline", "к'в", "ba`ck", EVERY_CHAR, "\x00\x01")
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
TYPED = (
    "select cast(1, 'Enum8(\\'a\\tb\\' = 1)') as e, "
    "cast(null, 'Nullable(String)') as ns, "
    "cast([(1, 'x')], 'Array(Tuple(UInt8, String))') as at, "
    "toDateTime64('2024-01-02 03:04:05', 3, 'UTC') as dt, "
    "cast(map('k', 1), 'Map(String, UInt32)') as m, "
    "toLowCardinality('x') as lc"
)
TYPED_NAMES = [
    "Enum8('a\\tb' = 1)",
    "Nullable(String)",
    "Array(Tuple(UInt8, String))",
    "DateTime64(3, 'UTC')",
    "Map(String, UInt32)",
    "LowCardinality(String)",
]
EXACT = (
    "select toFloat64('nan') as nan, toFloat64('-inf') as ninf, "
    "18446744073709551615 as u64, toFloat64('1e308') as f64, "
    "toDecimal64('-12345.6789', 4) as dec, unhex('ff00') as bin, 'a/b' as slash"
)
RECORDS = [{"n": 1, "s": "a\tb"}, {"s": "ё 中文", "n": 2, "extra": {"x": [1, 2]}}]
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


class Table:
    """Таблица в базе стенда форматов: пересоздаётся на тест, читается строками."""

    DATABASE: ClassVar[str] = "format stand"

    def __init__(self, name: str, columns: str) -> None:
        self._name = name
        self._columns = columns

    def insert(self) -> ChQuery:
        return (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s",
                db=ChIdentifier(self.DATABASE),
                t=ChIdentifier(self._name),
            )
            .build()
        )

    def select(self, columns: str | ChIdentifiers) -> ChQuery:
        """Выборка по порядку первой колонки: строка — голый текст колонок,
        ChIdentifiers — имена, которые квотирует драйвер."""
        if isinstance(columns, str):
            return (
                ChQueryBuilder()
                .add(
                    "select $columns from %(db)s.%(t)s order by 1",
                    columns=columns,
                    db=ChIdentifier(self.DATABASE),
                    t=ChIdentifier(self._name),
                )
                .build()
            )

        return (
            ChQueryBuilder()
            .add(
                "select %(columns)s from %(db)s.%(t)s order by 1",
                columns=columns,
                db=ChIdentifier(self.DATABASE),
                t=ChIdentifier(self._name),
            )
            .build()
        )

    async def recreate(self, client: Any) -> None:
        drop = (
            ChQueryBuilder()
            .add(
                "drop table if exists %(db)s.%(t)s",
                db=ChIdentifier(self.DATABASE),
                t=ChIdentifier(self._name),
            )
            .build()
        )
        create = (
            ChQueryBuilder()
            .add(
                "create table %(db)s.%(t)s ($columns) engine = Memory",
                db=ChIdentifier(self.DATABASE),
                t=ChIdentifier(self._name),
                columns=self._columns,
            )
            .build()
        )
        await client.command(drop.text, parameters=drop.params)
        await client.command(create.text, parameters=create.params)

    async def rows(
        self, client: Any, columns: str | ChIdentifiers
    ) -> list[tuple[Any, ...]]:
        query = self.select(columns)
        async with PayloadClickHouse.rows_stream_out(
            client, query.text, query.params
        ) as stream:
            return [tuple(row) async for row in stream.blocks]

    async def tsv(self, client: Any) -> bytes:
        query = self.select("*")
        data = bytearray()
        async with PayloadClickHouse.byte_stream_out(
            client, query.text, "TabSeparated", query.params
        ) as stream:
            async for block in stream.blocks:
                data.extend(block)

        return bytes(data)


def _weird_select(names: Sequence[str]) -> ChQuery:
    """Колонки с именами names и значениями 0, 1, 2, ... на три строки."""
    builder = ChQueryBuilder().add("select 0 as %(n0)s", n0=ChIdentifier(names[0]))
    for position in range(1, len(names)):
        key = f"n{position}"
        builder.add(
            f", {position} as %({key})s", **{key: ChIdentifier(names[position])}
        )

    builder.add("from numbers(3)")

    return builder.build()


def _uint8_table(name: str, names: Sequence[str]) -> ChQuery:
    """create table с колонками names типа UInt8."""
    builder = ChQueryBuilder().add(
        "create table %(db)s.%(t)s (%(c0)s UInt8",
        db=ChIdentifier(Table.DATABASE),
        t=ChIdentifier(name),
        c0=ChIdentifier(names[0]),
    )
    for position in range(1, len(names)):
        key = f"c{position}"
        builder.add(f", %({key})s UInt8", **{key: ChIdentifier(names[position])})

    builder.add(") engine = Memory")

    return builder.build()


async def _chunks(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


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
async def stand(source: StandSource) -> AsyncIterator[StandSource]:
    """База стенда форматов на источнике; после модуля сносится."""
    drop = (
        ChQueryBuilder()
        .add("drop database if exists %(db)s", db=ChIdentifier(Table.DATABASE))
        .build()
    )
    create = (
        ChQueryBuilder()
        .add("create database %(db)s", db=ChIdentifier(Table.DATABASE))
        .build()
    )
    async with PayloadClickHouse.opened_config(source.admin) as client:
        await client.command(drop.text, parameters=drop.params)
        await client.command(create.text, parameters=create.params)

    yield source

    async with PayloadClickHouse.opened_config(source.admin) as client:
        await client.command(drop.text, parameters=drop.params)


@pytest.fixture(scope="module")
def oldest_and_newest(raw_config: Any) -> tuple[StandSource, StandSource]:
    sources = bind(raw_config, path="ix_stand", model=StandSources)
    listed = [item for item in sources.ch_sources if item.demo]
    if len(listed) < 2:
        pytest.skip("ix_stand.ch_sources needs two sources with demo = true")

    return listed[0], listed[-1]


class Headed:
    """Чтение запроса через форматер с шапкой: имена, типы сервера, типы
    драйвера и данные после шапки."""

    def __init__(self, fmt: StreamFormat[Any]) -> None:
        self._fmt = fmt

    async def read(
        self, connection: ClickHouseConfig, query: ChQuery | str
    ) -> tuple[tuple[str, ...], tuple[str, ...], list[str], bytes]:
        if isinstance(query, str):
            text = query
            params = None
        else:
            text = query.text
            params = query.params

        async with (
            PayloadClickHouse.opened_config(connection) as client,
            PayloadClickHouse.byte_stream_out(
                client, text, self._fmt.FORMAT, params, self._fmt.output_settings(None)
            ) as raw,
        ):
            stream = await self._fmt.read(raw.blocks)
            data = bytearray()
            async for block in stream.blocks:
                data.extend(block)

        driver_types: list[str] = []
        for column_type in stream.column_types:
            driver_types.append(column_type.name)

        return stream.names, stream.type_names, driver_types, bytes(data)

    async def pipe(
        self, connection: ClickHouseConfig, select: ChQuery, insert: ChQuery
    ) -> int:
        """Выход одного запроса на вход другого через write; строк записано."""
        async with (
            PayloadClickHouse.opened_config(connection) as client,
            PayloadClickHouse.byte_stream_out(
                client,
                select.text,
                self._fmt.FORMAT,
                select.params,
                self._fmt.output_settings(None),
            ) as raw,
        ):
            stream = await self._fmt.read(raw.blocks)
            summary = await PayloadClickHouse.byte_stream_in(
                client,
                self._fmt.insert(insert.text),
                insert.params,
                self._fmt.input_settings(None),
                blocks=self._fmt.write(stream),
            )

        return summary.written_rows


FANCY = CustomSeparatedSpec(
    escaping_rule=EscapingRule.QUOTED,
    field_delimiter="|",
    row_before="<",
    row_after=">",
    row_between="\n",
    result_before="[\n",
    result_after="\n]\n",
)
HEADED = [
    TsvWithNamesAndTypes(),
    JsonCompactWithNamesAndTypes(),
    CsvWithNamesAndTypes(),
    CustomSeparatedWithNamesAndTypes(CustomSeparatedSpec()),
    CustomSeparatedWithNamesAndTypes(
        CustomSeparatedSpec(escaping_rule=EscapingRule.CSV, field_delimiter=";")
    ),
    CustomSeparatedWithNamesAndTypes(
        CustomSeparatedSpec(escaping_rule=EscapingRule.JSON, field_delimiter=", ")
    ),
]
QUOTELESS = [
    CustomSeparatedWithNamesAndTypes(
        CustomSeparatedSpec(escaping_rule=EscapingRule.RAW, field_delimiter="|")
    ),
    CustomSeparatedWithNamesAndTypes(
        CustomSeparatedSpec(escaping_rule=EscapingRule.XML, field_delimiter="|")
    ),
]


def _format_id(fmt: StreamFormat[Any]) -> str:
    settings = fmt.output_settings(None)
    rule = settings.get("format_custom_escaping_rule", "")
    delimiter = settings.get("format_custom_field_delimiter", "")

    return f"{fmt.FORMAT}{rule}{delimiter!r}"


@pytest.mark.integration
class TestHeadedOut:
    @pytest.fixture(params=HEADED, ids=_format_id)
    def headed(self, request: Any) -> Headed:
        return Headed(request.param)

    async def test_empty_result_still_has_a_header(
        self, headed: Headed, source: StandSource
    ) -> None:
        names, type_names, _, data = await headed.read(
            source.clickhouse, "select 1 as a, 'x' as b where 0"
        )

        assert names == ("a", "b")
        assert type_names == ("UInt8", "String")
        assert data == b""

    async def test_any_column_name_is_restored(
        self, headed: Headed, source: StandSource
    ) -> None:
        names, _, _, data = await headed.read(
            source.clickhouse, _weird_select(WEIRD_NAMES)
        )

        assert names == WEIRD_NAMES
        assert data.count(b"\n") == 3

    async def test_types_match_the_native_protocol(
        self, headed: Headed, source: StandSource
    ) -> None:
        names, type_names, driver_types, _ = await headed.read(source.clickhouse, TYPED)

        async with (
            PayloadClickHouse.opened_config(source.clickhouse) as client,
            PayloadClickHouse.rows_stream_out(client, TYPED) as stream,
        ):
            native: list[str] = []
            for column_type in stream.column_types:
                native.append(column_type.name)

        assert names == ("e", "ns", "at", "dt", "m", "lc")
        assert list(type_names) == TYPED_NAMES
        assert driver_types == native


@pytest.mark.integration
class TestTsvOut:
    async def test_rows_after_the_header_pass_as_is(self, source: StandSource) -> None:
        names, type_names, _, data = await Headed(TsvWithNamesAndTypes()).read(
            source.clickhouse,
            "select number as n, toString(number) as s from numbers(100000)",
        )

        expected = bytearray()
        for number in range(100000):
            expected.extend(f"{number}\t{number}\n".encode())

        assert names == ("n", "s")
        assert type_names == ("UInt64", "String")
        assert data == bytes(expected)


@pytest.mark.integration
class TestJsonCompactOut:
    async def test_rows_are_json_arrays(self, source: StandSource) -> None:
        _, type_names, _, data = await Headed(JsonCompactWithNamesAndTypes()).read(
            source.clickhouse, _weird_select(WEIRD_NAMES)
        )

        assert type_names == ("UInt8",) * len(WEIRD_NAMES)
        assert data == b"[0, 1, 2, 3, 4, 5, 6]\n" * 3

    async def test_bytes_are_the_same_across_versions(
        self, oldest_and_newest: tuple[StandSource, StandSource]
    ) -> None:
        oldest, newest = oldest_and_newest
        headed = Headed(JsonCompactWithNamesAndTypes())

        _, _, _, old = await headed.read(oldest.clickhouse, EXACT)
        _, _, _, new = await headed.read(newest.clickhouse, EXACT)

        assert old == new
        assert old == (
            b'["nan", "-inf", "18446744073709551615", "1e308", "-12345.6789", '
            b'"\xff\\u0000", "a\\/b"]\n'
        )


@pytest.mark.integration
class TestHeadedIn:
    """Выход read подаётся на вход write как есть: сервер сопоставляет колонки
    по именам из шапки."""

    @pytest.fixture(params=HEADED, ids=_format_id)
    def headed(self, request: Any) -> Headed:
        return Headed(request.param)

    async def test_columns_land_by_name(
        self, headed: Headed, stand: StandSource
    ) -> None:
        table = Table("by name", "")
        create = _uint8_table("by name", list(reversed(PIPE_NAMES)))
        drop = (
            ChQueryBuilder()
            .add(
                "drop table if exists %(db)s.%(t)s",
                db=ChIdentifier(Table.DATABASE),
                t=ChIdentifier("by name"),
            )
            .build()
        )
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await client.command(drop.text, parameters=drop.params)
            await client.command(create.text, parameters=create.params)

        written = await headed.pipe(
            stand.admin, _weird_select(PIPE_NAMES), table.insert()
        )

        async with PayloadClickHouse.opened_config(stand.admin) as client:
            rows = await table.rows(client, ChIdentifiers(PIPE_NAMES))

        expected = tuple(range(len(PIPE_NAMES)))

        assert written == 3
        assert rows == [expected, expected, expected]

    async def test_type_mismatch_is_refused(
        self, headed: Headed, stand: StandSource
    ) -> None:
        table = Table("typed", "n UInt64")
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await table.recreate(client)

        select = ChQueryBuilder().add("select toUInt32(1) as n").build()
        with pytest.raises(ClickHouseQueryError, match="must be UInt64"):
            await headed.pipe(stand.admin, select, table.insert())

    async def test_hard_values_survive_the_round_trip(
        self, headed: Headed, stand: StandSource
    ) -> None:
        """NaN, Inf, -0, 64-битные и 128/256-битные целые, Decimal, бинарные
        строки, FixedString, наносекунды и вложенные NaN: таблица после
        перекачки совпадает с исходной в TSV байт в байт."""
        origin = Table("exact src", HARD_COLUMNS)
        target = Table("exact dst", HARD_COLUMNS)
        fill = (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s $values",
                db=ChIdentifier(Table.DATABASE),
                t=ChIdentifier("exact src"),
                values=HARD_VALUES,
            )
            .build()
        )
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await origin.recreate(client)
            await target.recreate(client)
            await client.command(fill.text, parameters=fill.params)

        await headed.pipe(stand.admin, origin.select("*"), target.insert())

        async with PayloadClickHouse.opened_config(stand.admin) as client:
            before = await origin.tsv(client)
            after = await target.tsv(client)

        assert before.count(b"\n") == 7 * 3 * 2 * 3
        assert after == before


@pytest.mark.integration
class TestJsonLinesIn:
    @pytest.mark.parametrize(
        "document",
        [
            "\n".join(json.dumps(record, ensure_ascii=False) for record in RECORDS),
            json.dumps(RECORDS),
            json.dumps(RECORDS, indent=2, ensure_ascii=False),
        ],
        ids=["lines", "array", "pretty array"],
    )
    async def test_records_land_by_key(self, stand: StandSource, document: str) -> None:
        jsonl = JsonLines()
        table = Table("jsonl", "n UInt64, s String")
        insert = table.insert()
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await table.recreate(client)
            stream = await jsonl.read(_chunks(document.encode()))
            summary = await PayloadClickHouse.byte_stream_in(
                client,
                jsonl.insert(insert.text),
                insert.params,
                jsonl.input_settings(None),
                blocks=jsonl.write(stream),
            )
            rows = await table.rows(client, "n, s")

        assert summary.written_rows == 2
        assert rows == [(1, "a\tb"), (2, "ё 中文")]

    async def test_wrapped_records_are_refused_in_strict_mode(
        self, stand: StandSource
    ) -> None:
        jsonl = JsonLines()
        table = Table("jsonl strict", "n UInt64, s String")
        insert = table.insert()
        document = json.dumps({"items": RECORDS}).encode()
        strict = jsonl.input_settings({"input_format_skip_unknown_fields": 0})
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await table.recreate(client)
            with pytest.raises(ClickHouseQueryError, match="items"):
                await PayloadClickHouse.byte_stream_in(
                    client,
                    jsonl.insert(insert.text),
                    insert.params,
                    strict,
                    blocks=_chunks(document),
                )


@pytest.mark.integration
class TestJsonDocumentsIn:
    async def test_each_document_is_one_row(self, stand: StandSource) -> None:
        documents = JsonDocuments()
        table = Table("json doc", "doc String")
        insert = table.insert()
        first = json.dumps({"items": RECORDS, "meta": {"k": "v1"}}, indent=2)
        second = json.dumps({"items": RECORDS[:1], "meta": {"k": "v2"}})
        body = (first + "\n" + second).encode()
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await table.recreate(client)
            summary = await PayloadClickHouse.byte_stream_in(
                client,
                documents.insert(insert.text),
                insert.params,
                documents.input_settings(None),
                blocks=_chunks(body),
            )
            rows = await table.rows(
                client,
                "JSONExtractString(doc, 'meta', 'k'), "
                "length(JSONExtractArrayRaw(doc, 'items'))",
            )

        assert summary.written_rows == 2
        assert rows == [("v1", 2), ("v2", 1)]


@pytest.mark.integration
class TestCustomQuoteless:
    """Raw и XML разделители не экранируют: с именами без разделителей шапка
    читается, типы совпадают с Native. Обратный путь не проверяется: XML
    сервер не читает, Raw теряет составные значения."""

    @pytest.fixture(params=QUOTELESS, ids=_format_id)
    def headed(self, request: Any) -> Headed:
        return Headed(request.param)

    async def test_plain_names_and_types(
        self, headed: Headed, source: StandSource
    ) -> None:
        names, type_names, _, data = await headed.read(source.clickhouse, TYPED)

        assert names == ("e", "ns", "at", "dt", "m", "lc")
        assert list(type_names) == TYPED_NAMES
        assert data.count(b"\n") == 1


@pytest.mark.integration
class TestCustomSeparated:
    """Раскладка с result_before/result_after, row_before/row_after и
    row_between: шапка снимается, result_after остаётся в данных, а обратный
    путь через write восстанавливает целый документ."""

    async def test_empty_result_keeps_only_result_after(
        self, source: StandSource
    ) -> None:
        names, type_names, _, data = await Headed(
            CustomSeparatedWithNamesAndTypes(FANCY)
        ).read(source.clickhouse, "select 1 as a, 'x' as b where 0")

        assert names == ("a", "b")
        assert type_names == ("UInt8", "String")
        assert data == b"\n]\n"

    async def test_hard_values_survive_the_round_trip(self, stand: StandSource) -> None:
        headed = Headed(CustomSeparatedWithNamesAndTypes(FANCY))
        origin = Table("fancy src", HARD_COLUMNS)
        target = Table("fancy dst", HARD_COLUMNS)
        fill = (
            ChQueryBuilder()
            .add(
                "insert into %(db)s.%(t)s $values",
                db=ChIdentifier(Table.DATABASE),
                t=ChIdentifier("fancy src"),
                values=HARD_VALUES,
            )
            .build()
        )
        async with PayloadClickHouse.opened_config(stand.admin) as client:
            await origin.recreate(client)
            await target.recreate(client)
            await client.command(fill.text, parameters=fill.params)

        await headed.pipe(stand.admin, origin.select("*"), target.insert())

        async with PayloadClickHouse.opened_config(stand.admin) as client:
            before = await origin.tsv(client)
            after = await target.tsv(client)

        assert after == before

    async def test_names_only_land_by_name(self, stand: StandSource) -> None:
        names = CustomSeparatedWithNames(FANCY)
        table = Table("custom names", "")
        create = _uint8_table("custom names", list(reversed(PIPE_NAMES)))
        select = _weird_select(PIPE_NAMES)
        insert = table.insert()
        async with (
            PayloadClickHouse.opened_config(stand.admin) as client,
            PayloadClickHouse.byte_stream_out(
                client,
                select.text,
                names.FORMAT,
                select.params,
                names.output_settings(None),
            ) as raw,
        ):
            await client.command(create.text, parameters=create.params)
            stream = await names.read(raw.blocks)
            summary = await PayloadClickHouse.byte_stream_in(
                client,
                names.insert(insert.text),
                insert.params,
                names.input_settings(None),
                blocks=names.write(stream),
            )
            rows = await table.rows(client, ChIdentifiers(PIPE_NAMES))

        expected = tuple(range(len(PIPE_NAMES)))

        assert stream.names == PIPE_NAMES
        assert summary.written_rows == 3
        assert rows == [expected, expected, expected]

    async def test_headless_rows_pipe_by_position(self, stand: StandSource) -> None:
        plain = CustomSeparated(FANCY)
        table = Table("custom plain", "n UInt64, s String")
        insert = table.insert()
        async with (
            PayloadClickHouse.opened_config(stand.admin) as client,
            PayloadClickHouse.byte_stream_out(
                client,
                "select number, 'a|b<c>' from numbers(3)",
                plain.FORMAT,
                None,
                plain.output_settings(None),
            ) as raw,
        ):
            await table.recreate(client)
            stream = await plain.read(raw.blocks)
            data = bytearray()
            async for block in stream.blocks:
                data.extend(block)

            summary = await PayloadClickHouse.byte_stream_in(
                client,
                plain.insert(insert.text),
                insert.params,
                plain.input_settings(None),
                blocks=_chunks(bytes(data)),
            )
            rows = await table.rows(client, "n, s")

        assert bytes(data) == b"[\n<0|'a|b<c>'>\n<1|'a|b<c>'>\n<2|'a|b<c>'>\n]\n"
        assert summary.written_rows == 3
        assert rows == [(0, "a|b<c>"), (1, "a|b<c>"), (2, "a|b<c>")]


@pytest.mark.integration
class TestCsvWithNames:
    """Шапка из одних имён: пустой результат её шлёт, имена с запятой,
    кавычкой и переводом строки возвращаются как есть, обратный путь
    сопоставляет колонки по именам."""

    async def test_names_of_an_empty_result(self, source: StandSource) -> None:
        names = CsvWithNames()
        async with (
            PayloadClickHouse.opened_config(source.clickhouse) as client,
            PayloadClickHouse.byte_stream_out(
                client, "select 1 as a, 'x' as b where 0", names.FORMAT
            ) as raw,
        ):
            stream = await names.read(raw.blocks)
            data = bytearray()
            async for block in stream.blocks:
                data.extend(block)

        assert stream.names == ("a", "b")
        assert data == b""

    async def test_columns_land_by_name(self, stand: StandSource) -> None:
        names = CsvWithNames()
        table = Table("csv names", "")
        create = _uint8_table("csv names", list(reversed(PIPE_NAMES)))
        select = _weird_select(PIPE_NAMES)
        insert = table.insert()
        async with (
            PayloadClickHouse.opened_config(stand.admin) as client,
            PayloadClickHouse.byte_stream_out(
                client, select.text, names.FORMAT, select.params
            ) as raw,
        ):
            await client.command(create.text, parameters=create.params)
            stream = await names.read(raw.blocks)
            summary = await PayloadClickHouse.byte_stream_in(
                client,
                names.insert(insert.text),
                insert.params,
                blocks=names.write(stream),
            )
            rows = await table.rows(client, ChIdentifiers(PIPE_NAMES))

        expected = tuple(range(len(PIPE_NAMES)))

        assert stream.names == PIPE_NAMES
        assert summary.written_rows == 3
        assert rows == [expected, expected, expected]


@pytest.mark.integration
class TestCsv:
    async def test_rows_pipe_by_position(self, stand: StandSource) -> None:
        plain = Csv()
        table = Table("csv plain", "n UInt64, s String")
        insert = table.insert()
        async with (
            PayloadClickHouse.opened_config(stand.admin) as client,
            PayloadClickHouse.byte_stream_out(
                client,
                "select number, 'a\"b,c' from numbers(3)",
                plain.FORMAT,
            ) as raw,
        ):
            await table.recreate(client)
            stream = await plain.read(raw.blocks)
            summary = await PayloadClickHouse.byte_stream_in(
                client,
                plain.insert(insert.text),
                insert.params,
                blocks=plain.write(stream),
            )
            rows = await table.rows(client, "n, s")

        assert summary.written_rows == 3
        assert rows == [(0, 'a"b,c'), (1, 'a"b,c'), (2, 'a"b,c')]


class TestHeaderParsing:
    """Разбор шапки на потоках из памяти: границы блоков режут шапку где
    попало, обрыв и мусор в шапке — ошибка формата."""

    @pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
    async def test_tsv_header_split_across_chunks(self, size: int) -> None:
        body = b"id\tna\\tme\nUInt64\tString\n1\tx\n2\ty\n"

        stream = await TsvWithNamesAndTypes().read(_chunks(body, size))
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

        assert stream.names == ("id", "na\tme")
        assert stream.type_names == ("UInt64", "String")
        assert [column.name for column in stream.column_types] == ["UInt64", "String"]
        assert data == b"1\tx\n2\ty\n"

    @pytest.mark.parametrize("size", [1, 5, 1000])
    async def test_json_header_split_across_chunks(self, size: int) -> None:
        body = b'["id", "na\\tme"]\n["UInt64", "String"]\n[1, "x"]\n'

        stream = await JsonCompactWithNamesAndTypes().read(_chunks(body, size))
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

        assert stream.names == ("id", "na\tme")
        assert stream.type_names == ("UInt64", "String")
        assert data == b'[1, "x"]\n'

    @pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
    async def test_csv_header_split_across_chunks(self, size: int) -> None:
        body = b'"id","na\nme","qu""ote"\n"UInt64","String","UInt8"\n1,"x",2\n'

        stream = await CsvWithNamesAndTypes().read(_chunks(body, size))
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

        assert stream.names == ("id", "na\nme", 'qu"ote')
        assert stream.type_names == ("UInt64", "String", "UInt8")
        assert data == b'1,"x",2\n'

    async def test_csv_header_waits_for_the_line_end(self) -> None:
        body = b'"a","b"\n"UInt8","String"'

        with pytest.raises(ClickHouseFormatError, match="ended after 24 bytes"):
            await CsvWithNamesAndTypes().read(_chunks(body))

    async def test_csv_unclosed_quote_is_refused(self) -> None:
        with pytest.raises(ClickHouseFormatError, match="ended after"):
            await CsvWithNames().read(_chunks(b'"a,b\n'))

    @pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
    async def test_custom_header_split_across_chunks(self, size: int) -> None:
        body = (
            b"[\n<'id'|'pi|pe'|'qu\\'ote'>\n<'UInt64'|'String'|'UInt8'>\n<1|'x'|2>\n]\n"
        )

        stream = await CustomSeparatedWithNamesAndTypes(FANCY).read(_chunks(body, size))
        data = bytearray()
        async for block in stream.blocks:
            data.extend(block)

        assert stream.names == ("id", "pi|pe", "qu'ote")
        assert stream.type_names == ("UInt64", "String", "UInt8")
        assert data == b"<1|'x'|2>\n]\n"

    async def test_custom_layout_mismatch_is_refused(self) -> None:
        with pytest.raises(ClickHouseFormatError, match="expected result_before"):
            await CustomSeparatedWithNames(FANCY).read(_chunks(b"<'a'>\n"))

    async def test_custom_spec_without_row_end_is_refused(self) -> None:
        with pytest.raises(ClickHouseFormatError, match="rows cannot be told apart"):
            CustomSeparatedSpec(row_after="", row_between="")

    async def test_truncated_stream_is_refused(self) -> None:
        with pytest.raises(ClickHouseFormatError, match="ended after 14 bytes with 1"):
            await TsvWithNamesAndTypes().read(_chunks(b"id\tname\nUInt64"))

    async def test_unknown_type_is_refused(self) -> None:
        with pytest.raises(ClickHouseFormatError, match="NoSuchType"):
            await TsvWithNamesAndTypes().read(_chunks(b"id\nNoSuchType(1)\n1\n"))

    async def test_json_header_that_is_not_an_array_is_refused(self) -> None:
        with pytest.raises(ClickHouseFormatError, match="JSON array of strings"):
            await JsonCompactWithNamesAndTypes().read(_chunks(b'{"a": 1}\n[]\n'))

    def test_write_puts_the_header_back(self) -> None:
        tsv = TsvWithNamesAndTypes()
        assert tsv.insert("insert into t") == (
            "insert into t\n FORMAT TabSeparatedWithNamesAndTypes"
        )
