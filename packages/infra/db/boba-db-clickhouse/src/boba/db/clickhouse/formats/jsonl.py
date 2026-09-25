"""Формат JSONEachRow: по JSON-объекту на запись. На вход сервер принимает
как есть и потоком JSON Lines, один JSON-массив объектов (в строку или
отформатированный) и одиночный объект."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import LineHead, Settings

__all__ = ["JsonLines", "JsonLinesStream"]


@dataclass(frozen=True)
class JsonLinesStream:
    """Поток JSONEachRow: байты как есть, шапки у формата нет."""

    blocks: AsyncIterator[memoryview]


class JsonLines(StreamFormat[JsonLinesStream]):
    """Реализация StreamFormat для JSONEachRow: разбирать нечего, read и write
    отдают байты как есть.

    На вставке колонки сопоставляются по ключам объекта; ключ, которого нет в
    таблице, по умолчанию молча пропускается, а отсутствующий ключ даёт
    значение колонки по умолчанию. Из-за этого документ, где записи лежат не
    в корне ({"items": [...]}), молча станет одной строкой из умолчаний;
    input_format_skip_unknown_fields = 0 в input_settings делает это ошибкой.
    Такие документы принимает JsonDocuments."""

    FORMAT: ClassVar[str] = "JSONEachRow"

    def __init__(self) -> None:
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> JsonLinesStream:
        return JsonLinesStream(blocks=LineHead.views(blocks))

    def write(self, stream: JsonLinesStream) -> AsyncIterator[memoryview]:
        return stream.blocks
