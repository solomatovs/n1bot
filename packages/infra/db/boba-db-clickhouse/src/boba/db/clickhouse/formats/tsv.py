"""Формат TabSeparatedWithNamesAndTypes: имена и типы колонок в первых двух
строках, дальше по строке на запись. Экранирование TabSeparated совпадает с
текстовым форматом COPY PostgreSQL, NULL это \\N.

Ошибки:
ClickHouseFormatError — поток оборвался до шапки или в ней незнакомый тип.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from clickhouse_connect.datatypes.base import ClickHouseType

from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import ColumnTypes, LineHead, Settings

__all__ = ["TsvStream", "TsvWithNamesAndTypes"]


@dataclass(frozen=True)
class TsvStream:
    """Поток TabSeparatedWithNamesAndTypes без шапки: имена колонок, типы так,
    как их написал сервер (они же уходят обратно в шапку при записи), те же
    типы объектами драйвера и байты строк."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


class TsvEscape(StrEnum):
    """Буквы escape-последовательностей TabSeparated, означающие управляющий
    символ. Кроме них ClickHouse экранирует только `\\\\` и `\\'`, где символ после
    слэша означает сам себя; всё остальное, включая прочие управляющие символы
    и не-ASCII, пишется как есть."""

    TAB = "t"
    NEWLINE = "n"
    RETURN = "r"
    BACKSPACE = "b"
    FORM_FEED = "f"
    NUL = "0"

    def char(self) -> str:
        match self:
            case TsvEscape.TAB:
                return "\t"
            case TsvEscape.NEWLINE:
                return "\n"
            case TsvEscape.RETURN:
                return "\r"
            case TsvEscape.BACKSPACE:
                return "\b"
            case TsvEscape.FORM_FEED:
                return "\f"
            case TsvEscape.NUL:
                return "\0"


class TsvHeader:
    """Разбор и запись строки шапки (имён или типов). Сырая табуляция в шапке
    бывает только разделителем, потому что табуляцию внутри имени сервер
    пишет как `\\t`; поэтому строка сначала делится по табуляции, а потом в
    каждом имени раскрываются escape-последовательности. Запись экранирует
    ровно те символы, которые экранирует сам сервер."""

    SEPARATOR: ClassVar[str] = "\t"
    ESCAPE: ClassVar[str] = "\\"
    ENCODING: ClassVar[str] = "utf-8"
    LINE_END: ClassVar[bytes] = b"\n"
    QUOTE: ClassVar[str] = "'"

    def __init__(self) -> None:
        self._escapes: dict[str, str] = {}
        for escape in TsvEscape:
            self._escapes[escape.char()] = self.ESCAPE + escape.value

        self._escapes[self.ESCAPE] = self.ESCAPE + self.ESCAPE
        self._escapes[self.QUOTE] = self.ESCAPE + self.QUOTE

    def render(self, values: Sequence[str]) -> bytes:
        fields: list[str] = []
        for value in values:
            fields.append(self._escaped(value))

        line = self.SEPARATOR.join(fields)

        return line.encode(self.ENCODING) + self.LINE_END

    def parse(self, line: bytes) -> tuple[str, ...]:
        text = line.decode(self.ENCODING)

        names: list[str] = []
        for field in text.split(self.SEPARATOR):
            names.append(self._unescaped(field))

        return tuple(names)

    def _unescaped(self, field: str) -> str:
        if self.ESCAPE not in field:
            return field

        chars: list[str] = []
        escaped = False
        for char in field:
            if escaped:
                chars.append(self._escape_of(char))
                escaped = False
                continue

            if char == self.ESCAPE:
                escaped = True
                continue

            chars.append(char)

        return "".join(chars)

    def _escape_of(self, char: str) -> str:
        try:
            escape = TsvEscape(char)
        except ValueError:
            return char

        return escape.char()

    def _escaped(self, value: str) -> str:
        chars: list[str] = []
        for char in value:
            chars.append(self._escapes.get(char, char))

        return "".join(chars)


class TsvWithNamesAndTypes(StreamFormat[TsvStream]):
    """Реализация StreamFormat для TabSeparatedWithNamesAndTypes.

    read снимает две строки шапки, строит типы драйвером и отдаёт остальные
    байты как есть; write ставит шапку из имён и типов потока перед его
    байтами. Сервер на вставке сопоставляет колонки по именам из шапки,
    отклоняет несовпавший тип и по умолчанию молча пропускает лишнюю колонку
    (input_format_skip_unknown_fields = 1)."""

    FORMAT: ClassVar[str] = "TabSeparatedWithNamesAndTypes"

    def __init__(self) -> None:
        self._header = TsvHeader()
        self._head = LineHead(self.FORMAT, 2)
        self._types = ColumnTypes(self.FORMAT)
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> TsvStream:
        chunks = LineHead.views(blocks)
        head = await self._head.take(chunks)
        names = self._header.parse(head.lines[0])
        type_names = self._header.parse(head.lines[1])

        return TsvStream(
            names=names,
            type_names=type_names,
            column_types=self._types.of(type_names),
            blocks=LineHead.glued(head.rest, chunks),
        )

    def write(self, stream: TsvStream) -> AsyncIterator[memoryview]:
        head = self._header.render(stream.names) + self._header.render(
            stream.type_names
        )

        return LineHead.glued(head, stream.blocks)
