"""Формат JSONAsString: каждый JSON-документ верхнего уровня из тела — одна
запись, документ целиком ложится в единственную колонку String (или в
колонку, названную в `INSERT INTO t (doc)`); разбирают его потом в SQL
функциями JSONExtract*. Документы в теле идут подряд, через перевод строки
или без него. Формат только на вход."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import Lines, Settings

__all__ = ["JsonDocuments", "JsonDocumentsStream"]


@dataclass(frozen=True)
class JsonDocumentsStream:
    """Поток произвольных JSON-документов: байты как есть."""

    blocks: AsyncIterator[memoryview]


class JsonDocuments(StreamFormat[JsonDocumentsStream]):
    """Реализация StreamFormat для JSONAsString: разбирать нечего, read и write
    отдают байты как есть."""

    FORMAT: ClassVar[str] = "JSONAsString"

    def __init__(self) -> None:
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> JsonDocumentsStream:
        return JsonDocumentsStream(blocks=Lines.views(blocks))

    def write(self, stream: JsonDocumentsStream) -> AsyncIterator[memoryview]:
        return stream.blocks
