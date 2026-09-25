"""Форматы потоков ClickHouse: каждый форматер знает имя своего FORMAT,
настройки сервера для него, свой FORMAT-хвост для INSERT и как обернуть
байтовый поток в поток формата и обратно. Форматеры не зависят друг от
друга и оборачивают любой поток байт, не только ответ PayloadClickHouse.

Ошибки:
ClickHouseFormatError — поток оборвался до шапки, шапка не разбирается или
    в ней незнакомый тип.
"""

from __future__ import annotations

from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.blob import RawBlob, RawBlobStream
from boba.db.clickhouse.formats.json_compact import (
    JsonCompactStream,
    JsonCompactWithNamesAndTypes,
    JsonExactOutput,
)
from boba.db.clickhouse.formats.json_document import JsonDocuments, JsonDocumentsStream
from boba.db.clickhouse.formats.jsonl import JsonLines, JsonLinesStream
from boba.db.clickhouse.formats.tsv import TsvStream, TsvWithNamesAndTypes

__all__ = [
    "Blocks",
    "JsonCompactStream",
    "JsonCompactWithNamesAndTypes",
    "JsonDocuments",
    "JsonDocumentsStream",
    "JsonExactOutput",
    "JsonLines",
    "JsonLinesStream",
    "RawBlob",
    "RawBlobStream",
    "StreamFormat",
    "TsvStream",
    "TsvWithNamesAndTypes",
]
