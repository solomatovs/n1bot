"""Формат JSONCompactEachRowWithNamesAndTypes: имена и типы колонок в первых
двух строках JSON-массивами, дальше по JSON-массиву на запись. Настройки
вывода делают его определённым: путь через него обратно в ClickHouse
совпадает с TSV байт в байт, а 22.x и новые версии пишут одинаковые байты.

Ошибки:
ClickHouseFormatError — поток оборвался до шапки, шапка не JSON-массив
    строк или в ней незнакомый тип.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from clickhouse_connect.datatypes.base import ClickHouseType
from pydantic import TypeAdapter, ValidationError

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import (
    ColumnTypes,
    JsonExactOutput,
    Lines,
    Settings,
)

__all__ = ["JsonCompactStream", "JsonCompactWithNamesAndTypes"]


@dataclass(frozen=True)
class JsonCompactStream:
    """Поток JSONCompactEachRowWithNamesAndTypes без шапки: имена колонок,
    типы так, как их написал сервер (они же уходят обратно в шапку при
    записи), те же типы объектами драйвера и байты строк."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


class JsonCompactHeader:
    """Строка шапки — JSON-массив строк: разбирает и проверяет его pydantic,
    пишет json.dumps; экранирование целиком по правилам JSON."""

    LINE_END: ClassVar[bytes] = b"\n"
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, fmt: str) -> None:
        self._fmt = fmt
        self._values = TypeAdapter(tuple[str, ...])

    def parse(self, line: bytes) -> tuple[str, ...]:
        try:
            return self._values.validate_json(line)
        except ValidationError as exc:
            raise ClickHouseFormatError(
                f"reading {self._fmt} header: expected a JSON array of strings, "
                f"got {line[:200]!r}: {exc}"
            ) from exc

    def render(self, values: Sequence[str]) -> bytes:
        listed = list(values)
        text = json.dumps(listed, ensure_ascii=False)

        return text.encode(self.ENCODING) + self.LINE_END


class JsonCompactWithNamesAndTypes(StreamFormat[JsonCompactStream]):
    """Реализация StreamFormat для JSONCompactEachRowWithNamesAndTypes.

    read снимает две строки шапки, строит типы драйвером и отдаёт остальные
    байты как есть; write ставит шапку из имён и типов потока перед его
    байтами. output_settings — JsonExactOutput, extra вызывающего их
    перекрывает, и тогда определённость на его совести. Сервер на вставке
    сопоставляет колонки по именам, отклоняет несовпавший тип и по умолчанию
    молча пропускает лишнюю колонку (input_format_skip_unknown_fields = 1).
    Строки с невалидным UTF-8 идут сырыми байтами: ClickHouse примет их
    обратно, строгий JSON-парсер — нет."""

    FORMAT: ClassVar[str] = "JSONCompactEachRowWithNamesAndTypes"

    def __init__(self) -> None:
        self._header = JsonCompactHeader(self.FORMAT)
        self._head = Lines(self.FORMAT, 2)
        self._types = ColumnTypes(self.FORMAT)

        exact: dict[str, Any] = {}
        for setting in JsonExactOutput:
            exact[setting.value] = setting.value_of()

        self._output = Settings(exact)
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> JsonCompactStream:
        chunks = Lines.views(blocks)
        head = await self._head.take(chunks)
        names = self._header.parse(head.lines[0])
        type_names = self._header.parse(head.lines[1])

        return JsonCompactStream(
            names=names,
            type_names=type_names,
            column_types=self._types.of(type_names),
            blocks=Lines.all(head.rest, chunks),
        )

    def write(self, stream: JsonCompactStream) -> AsyncIterator[memoryview]:
        head = self._header.render(stream.names) + self._header.render(
            stream.type_names
        )

        return Lines.all(head, stream.blocks)
