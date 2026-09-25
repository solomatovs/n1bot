"""Формат CustomSeparated и подвиды с именами (CustomSeparatedWithNames) и
именами и типами (CustomSeparatedWithNamesAndTypes). Раскладку задаёт
CustomSeparatedSpec: правило экранирования полей и пять разделителей, они
же уходят серверу настройками format_custom_*. Ответ устроен так:
result_before, каждая строка (и шапки, и данных) как row_before + поля через
field_delimiter + row_after, между строками row_between, в конце
result_after; у пустого результата шапка есть.

Правила Quoted, CSV и JSON заключают поле в кавычки, и любой разделитель
внутри имени безопасен. Escaped экранирует только управляющие символы,
`\\\\` и `\\'`, а Raw и XML разделители не экранируют вовсе: с ними имя,
содержащее разделитель, разобрать нельзя, и spec обязан выбрать
разделители, которых в именах нет. XML сервер только пишет, вставку с ним
он отклоняет; Raw теряет составные значения (Tuple, Map) на обратном пути.
JSON пишет значения с настройками JsonExactOutput, чтобы NaN, Inf и
64-битные числа проходили без потерь.

Ошибки:
ClickHouseFormatError — поток оборвался до конца шапки, байты не ложатся в
    раскладку spec, поле не разбирается по правилу или в шапке незнакомый тип.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape

from clickhouse_connect.datatypes.base import ClickHouseType

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import (
    BackslashEscapes,
    ColumnTypes,
    JsonExactOutput,
    Lines,
    Settings,
)

__all__ = [
    "CustomNamesStream",
    "CustomSeparated",
    "CustomSeparatedSpec",
    "CustomSeparatedWithNames",
    "CustomSeparatedWithNamesAndTypes",
    "CustomStream",
    "CustomTypedStream",
    "EscapingRule",
]

ENCODING = "utf-8"


@dataclass(frozen=True)
class CustomStream:
    """Поток CustomSeparated без шапки: байты как есть, с result_before."""

    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class CustomNamesStream:
    """Поток CustomSeparatedWithNames без result_before и шапки: имена колонок
    и байты строк данных с result_after в конце."""

    names: tuple[str, ...]
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class CustomTypedStream:
    """Поток CustomSeparatedWithNamesAndTypes без result_before и шапки: имена
    колонок, типы так, как их написал сервер, те же типы объектами драйвера и
    байты строк данных с result_after в конце."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


class FieldRule(Protocol):
    """Поле шапки под одним правилом экранирования: где оно кончается в
    буфере, как читается и как пишется. Реализации выбирает EscapingRule."""

    @abstractmethod
    def end_of(self, buffer: bytes, start: int, stops: Sequence[bytes]) -> int | None:
        """Индекс байта сразу за полем, начатым в start; None — в буфере поля
        ещё нет целиком. stops — байты, которыми поле может кончаться, для
        правил без кавычек."""

    @abstractmethod
    def decode(self, raw: bytes) -> str: ...

    @abstractmethod
    def encode(self, value: str) -> bytes: ...

    @abstractmethod
    def output_settings(self) -> dict[str, Any]:
        """Настройки сервера, при которых вывод под этим правилом определён."""


class StoppedField(FieldRule):
    """База правил без кавычек: поле кончается на ближайшем разделителе;
    наследники задают только чтение и запись значения."""

    def end_of(self, buffer: bytes, start: int, stops: Sequence[bytes]) -> int | None:
        found: list[int] = []
        for stop in stops:
            index = buffer.find(stop, start)
            if index >= 0:
                found.append(index)

        if not found:
            return None

        return min(found)

    @abstractmethod
    def decode(self, raw: bytes) -> str: ...

    @abstractmethod
    def encode(self, value: str) -> bytes: ...

    def output_settings(self) -> dict[str, Any]:
        return {}


class EscapedField(StoppedField):
    """Правило Escaped: как TabSeparated."""

    def __init__(self) -> None:
        self._escapes = BackslashEscapes()

    def decode(self, raw: bytes) -> str:
        return self._escapes.unescaped(raw.decode(ENCODING))

    def encode(self, value: str) -> bytes:
        return self._escapes.escaped(value).encode(ENCODING)


class RawField(StoppedField):
    """Правило Raw: байты как есть."""

    def decode(self, raw: bytes) -> str:
        return raw.decode(ENCODING)

    def encode(self, value: str) -> bytes:
        return value.encode(ENCODING)


class XmlField(StoppedField):
    """Правило XML: `<` и `&` сущностями."""

    def decode(self, raw: bytes) -> str:
        return xml_unescape(raw.decode(ENCODING))

    def encode(self, value: str) -> bytes:
        return xml_escape(value).encode(ENCODING)


class QuotedField(FieldRule):
    """Правило Quoted: строка в одинарных кавычках с backslash-экранированием."""

    QUOTE: ClassVar[int] = ord("'")
    BACKSLASH: ClassVar[int] = ord("\\")

    def __init__(self) -> None:
        self._escapes = BackslashEscapes()

    def end_of(self, buffer: bytes, start: int, stops: Sequence[bytes]) -> int | None:
        if buffer[start] != self.QUOTE:
            raise ClickHouseFormatError(
                f"reading a Quoted header field: expected a single quote, got "
                f"{buffer[start : start + 20]!r}"
            )

        index = start + 1
        while index < len(buffer):
            byte = buffer[index]
            if byte == self.BACKSLASH:
                index += 2
                continue

            if byte == self.QUOTE:
                return index + 1

            index += 1

        return None

    def decode(self, raw: bytes) -> str:
        return self._escapes.unescaped(raw[1:-1].decode(ENCODING))

    def encode(self, value: str) -> bytes:
        return ("'" + self._escapes.escaped(value) + "'").encode(ENCODING)

    def output_settings(self) -> dict[str, Any]:
        return {}


class CsvField(FieldRule):
    """Правило CSV: строка в двойных кавычках, кавычка внутри удваивается."""

    QUOTE: ClassVar[int] = ord('"')

    def end_of(self, buffer: bytes, start: int, stops: Sequence[bytes]) -> int | None:
        if buffer[start] != self.QUOTE:
            raise ClickHouseFormatError(
                f"reading a CSV header field: expected a double quote, got "
                f"{buffer[start : start + 20]!r}"
            )

        index = start + 1
        while True:
            closing = buffer.find(b'"', index)
            if closing < 0 or closing + 1 >= len(buffer):
                return None

            if buffer[closing + 1] == self.QUOTE:
                index = closing + 2
                continue

            return closing + 1

    def decode(self, raw: bytes) -> str:
        return raw[1:-1].decode(ENCODING).replace('""', '"')

    def encode(self, value: str) -> bytes:
        return ('"' + value.replace('"', '""') + '"').encode(ENCODING)

    def output_settings(self) -> dict[str, Any]:
        return {}


class JsonField(FieldRule):
    """Правило JSON: строка JSON в двойных кавычках."""

    QUOTE: ClassVar[int] = ord('"')
    BACKSLASH: ClassVar[int] = ord("\\")

    def end_of(self, buffer: bytes, start: int, stops: Sequence[bytes]) -> int | None:
        if buffer[start] != self.QUOTE:
            raise ClickHouseFormatError(
                f"reading a JSON header field: expected a double quote, got "
                f"{buffer[start : start + 20]!r}"
            )

        index = start + 1
        while index < len(buffer):
            byte = buffer[index]
            if byte == self.BACKSLASH:
                index += 2
                continue

            if byte == self.QUOTE:
                return index + 1

            index += 1

        return None

    def decode(self, raw: bytes) -> str:
        value = json.loads(raw.decode(ENCODING))
        if not isinstance(value, str):
            raise ClickHouseFormatError(
                f"reading a JSON header field: expected a string, got {raw[:40]!r}"
            )

        return value

    def encode(self, value: str) -> bytes:
        return json.dumps(value, ensure_ascii=False).encode(ENCODING)

    def output_settings(self) -> dict[str, Any]:
        exact: dict[str, Any] = {}
        for setting in JsonExactOutput:
            exact[setting.value] = setting.value_of()

        return exact


class EscapingRule(StrEnum):
    """Правило экранирования полей CustomSeparated: значение настройки
    format_custom_escaping_rule."""

    ESCAPED = "Escaped"
    QUOTED = "Quoted"
    CSV = "CSV"
    JSON = "JSON"
    RAW = "Raw"
    XML = "XML"

    def field(self) -> FieldRule:
        match self:
            case EscapingRule.ESCAPED:
                return EscapedField()
            case EscapingRule.QUOTED:
                return QuotedField()
            case EscapingRule.CSV:
                return CsvField()
            case EscapingRule.JSON:
                return JsonField()
            case EscapingRule.RAW:
                return RawField()
            case EscapingRule.XML:
                return XmlField()


class CustomSetting(StrEnum):
    """Настройки сервера, задающие раскладку CustomSeparated."""

    ESCAPING_RULE = "format_custom_escaping_rule"
    FIELD_DELIMITER = "format_custom_field_delimiter"
    ROW_BEFORE = "format_custom_row_before_delimiter"
    ROW_AFTER = "format_custom_row_after_delimiter"
    ROW_BETWEEN = "format_custom_row_between_delimiter"
    RESULT_BEFORE = "format_custom_result_before_delimiter"
    RESULT_AFTER = "format_custom_result_after_delimiter"


@dataclass(frozen=True)
class CustomSeparatedSpec:
    """Раскладка CustomSeparated; умолчания — умолчания сервера, при которых
    формат совпадает с TabSeparated. Строка кончается на row_after +
    row_between, и хотя бы одно из них обязано быть непустым, иначе строки
    не отличить."""

    escaping_rule: EscapingRule = EscapingRule.ESCAPED
    field_delimiter: str = "\t"
    row_before: str = ""
    row_after: str = "\n"
    row_between: str = ""
    result_before: str = ""
    result_after: str = ""

    def __post_init__(self) -> None:
        if not self.field_delimiter:
            raise ClickHouseFormatError(
                "custom separated spec: field_delimiter must not be empty"
            )

        if not self.row_end():
            raise ClickHouseFormatError(
                "custom separated spec: row_after and row_between are both empty, "
                "rows cannot be told apart"
            )

    def row_end(self) -> str:
        """Что стоит после каждой строки шапки: row_after и row_between."""
        return self.row_after + self.row_between

    def output_settings(self) -> dict[str, Any]:
        """Настройки вывода: раскладка плюс настройки правила экранирования."""
        chosen = self.settings()
        chosen.update(self.escaping_rule.field().output_settings())

        return chosen

    def settings(self) -> dict[str, Any]:
        return {
            CustomSetting.ESCAPING_RULE.value: self.escaping_rule.value,
            CustomSetting.FIELD_DELIMITER.value: self.field_delimiter,
            CustomSetting.ROW_BEFORE.value: self.row_before,
            CustomSetting.ROW_AFTER.value: self.row_after,
            CustomSetting.ROW_BETWEEN.value: self.row_between,
            CustomSetting.RESULT_BEFORE.value: self.result_before,
            CustomSetting.RESULT_AFTER.value: self.result_after,
        }


@dataclass(frozen=True)
class CustomRows:
    """Разобранные строки шапки и байты после них."""

    rows: tuple[tuple[str, ...], ...]
    rest: bytes


class CustomHead:
    """Снимает result_before и заданное число строк шапки с начала байтового
    потока по раскладке spec, разбирая поля правилом экранирования; остальное
    идёт дальше как есть. На входе пишет ту же шапку перед потоком."""

    def __init__(self, fmt: str, spec: CustomSeparatedSpec, count: int) -> None:
        self._fmt = fmt
        self._spec = spec
        self._count = count
        self._field = spec.escaping_rule.field()
        self._result_before = spec.result_before.encode(ENCODING)
        self._row_before = spec.row_before.encode(ENCODING)
        self._delimiter = spec.field_delimiter.encode(ENCODING)
        self._row_end = spec.row_end().encode(ENCODING)
        self._stops: list[bytes] = [self._delimiter, self._row_end]

    async def take(self, chunks: AsyncIterator[memoryview]) -> CustomRows:
        buffer = bytearray()
        while True:
            parsed = self._parsed(bytes(buffer))
            if parsed is not None:
                return parsed

            chunk = await anext(chunks, None)
            if chunk is None:
                raise ClickHouseFormatError(
                    f"reading {self._fmt} header: expected {self._count} header "
                    f"rows, the stream ended after {len(buffer)} bytes"
                )

            buffer.extend(chunk)

    def render(self, rows: Sequence[Sequence[str]]) -> bytes:
        head = bytearray(self._result_before)
        for row in rows:
            fields: list[bytes] = []
            for value in row:
                fields.append(self._field.encode(value))

            head.extend(self._row_before)
            head.extend(self._delimiter.join(fields))
            head.extend(self._row_end)

        return bytes(head)

    def _parsed(self, buffer: bytes) -> CustomRows | None:
        position = self._expected(buffer, 0, self._result_before, "result_before")
        if position is None:
            return None

        rows: list[tuple[str, ...]] = []
        for _ in range(self._count):
            row = self._row(buffer, position)
            if row is None:
                return None

            fields, position = row
            rows.append(fields)

        return CustomRows(rows=tuple(rows), rest=buffer[position:])

    def _row(self, buffer: bytes, position: int) -> tuple[tuple[str, ...], int] | None:
        start = self._expected(buffer, position, self._row_before, "row_before")
        if start is None:
            return None

        fields: list[str] = []
        while True:
            if start >= len(buffer):
                return None

            end = self._field.end_of(buffer, start, self._stops)
            if end is None:
                return None

            fields.append(self._field.decode(buffer[start:end]))
            if buffer.startswith(self._row_end, end):
                return tuple(fields), end + len(self._row_end)

            if buffer.startswith(self._delimiter, end):
                start = end + len(self._delimiter)
                continue

            longest = max(len(self._row_end), len(self._delimiter))
            if len(buffer) - end < longest:
                return None

            raise ClickHouseFormatError(
                f"reading {self._fmt} header: expected the field delimiter "
                f"{self._spec.field_delimiter!r} or the row end "
                f"{self._spec.row_end()!r} after a field, got "
                f"{buffer[end : end + 20]!r}"
            )

    def _expected(
        self, buffer: bytes, position: int, prefix: bytes, what: str
    ) -> int | None:
        if buffer.startswith(prefix, position):
            return position + len(prefix)

        tail = buffer[position:]
        if len(tail) < len(prefix) and prefix.startswith(tail):
            return None

        raise ClickHouseFormatError(
            f"reading {self._fmt} header: expected {what} {prefix!r} at byte "
            f"{position}, got {tail[:20]!r}"
        )


class CustomSeparated(StreamFormat[CustomStream]):
    """Реализация StreamFormat для CustomSeparated без шапки: раскладку задаёт
    spec, разбирать нечего, read и write отдают байты как есть."""

    FORMAT: ClassVar[str] = "CustomSeparated"

    def __init__(self, spec: CustomSeparatedSpec) -> None:
        self._output = Settings(spec.output_settings())
        self._input = Settings(spec.settings())

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> CustomStream:
        return CustomStream(blocks=Lines.views(blocks))

    def write(self, stream: CustomStream) -> AsyncIterator[memoryview]:
        return stream.blocks


class CustomSeparatedWithNames(StreamFormat[CustomNamesStream]):
    """Реализация StreamFormat для CustomSeparatedWithNames: read снимает
    result_before и строку имён, write ставит их перед байтами потока.
    Сервер на вставке сопоставляет колонки по именам."""

    FORMAT: ClassVar[str] = "CustomSeparatedWithNames"

    def __init__(self, spec: CustomSeparatedSpec) -> None:
        self._head = CustomHead(self.FORMAT, spec, 1)
        self._output = Settings(spec.output_settings())
        self._input = Settings(spec.settings())

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> CustomNamesStream:
        chunks = Lines.views(blocks)
        head = await self._head.take(chunks)

        return CustomNamesStream(
            names=head.rows[0], blocks=Lines.all(head.rest, chunks)
        )

    def write(self, stream: CustomNamesStream) -> AsyncIterator[memoryview]:
        return Lines.all(self._head.render([stream.names]), stream.blocks)


class CustomSeparatedWithNamesAndTypes(StreamFormat[CustomTypedStream]):
    """Реализация StreamFormat для CustomSeparatedWithNamesAndTypes: read
    снимает result_before, строки имён и типов и строит типы драйвером;
    write ставит их перед байтами потока. Сервер на вставке сопоставляет
    колонки по именам и отклоняет несовпавший тип."""

    FORMAT: ClassVar[str] = "CustomSeparatedWithNamesAndTypes"

    def __init__(self, spec: CustomSeparatedSpec) -> None:
        self._head = CustomHead(self.FORMAT, spec, 2)
        self._types = ColumnTypes(self.FORMAT)
        self._output = Settings(spec.output_settings())
        self._input = Settings(spec.settings())

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> CustomTypedStream:
        chunks = Lines.views(blocks)
        head = await self._head.take(chunks)
        names = head.rows[0]
        type_names = head.rows[1]

        return CustomTypedStream(
            names=names,
            type_names=type_names,
            column_types=self._types.of(type_names),
            blocks=Lines.all(head.rest, chunks),
        )

    def write(self, stream: CustomTypedStream) -> AsyncIterator[memoryview]:
        head = self._head.render([stream.names, stream.type_names])

        return Lines.all(head, stream.blocks)
