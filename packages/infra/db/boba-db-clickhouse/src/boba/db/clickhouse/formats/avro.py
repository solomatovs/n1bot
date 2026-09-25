"""Форматы Avro: контейнер Avro и AvroConfluent. Контейнер начинается с
заголовка по спецификации Avro: магия Obj1, карта метаданных (avro.schema —
схема записи JSON, avro.codec — сжатие блоков) и 16-байтовый маркер
синхронизации; дальше блоки записей, каждый с тем же маркером в конце.
Рамка заголовка снимается по спецификации (fastavro читает заголовок только
вместе с началом первого блока и обрыв от чужих байтов не отличает), а саму
схему разбирает и проверяет fastavro; имена и типы колонок — из неё. Байты
заголовка сохраняются как есть, и write ставит их обратно ровно такими,
поэтому маркер блоков совпадает. Сервер отдаёт контейнер потоком и
принимает потоком.

AvroConfluent — сообщения Kafka со схемой из реестра Confluent: без реестра
их не разобрать, поэтому форматер пропускает байты как есть, а адрес реестра
уходит серверу настройкой. Старые серверы этот формат только читают.

Ошибки:
ClickHouseFormatError — поток оборвался до конца заголовка, начало не
    ложится в раскладку контейнера или схема не разбирается.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from fastavro import parse_schema
from fastavro.schema import SchemaParseException

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import Lines, Settings

__all__ = [
    "Avro",
    "AvroColumns",
    "AvroConfluent",
    "AvroConfluentStream",
]


@dataclass(frozen=True)
class AvroColumns:
    """Поток контейнера Avro без заголовка: имена колонок, схема записи в
    разборе fastavro (type, name, fields с именем и типом Avro), кодек
    блоков, байты заголовка (write ставит их обратно) и байты блоков."""

    names: tuple[str, ...]
    schema: Mapping[str, Any]
    codec: str
    head: bytes
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class AvroConfluentStream:
    """Поток AvroConfluent: сообщения как есть, схему знает только реестр."""

    blocks: AsyncIterator[memoryview]


class AvroMetadata(StrEnum):
    """Ключи карты метаданных контейнера."""

    SCHEMA = "avro.schema"
    CODEC = "avro.codec"


class AvroSetting(StrEnum):
    """Настройки сервера для AvroConfluent."""

    REGISTRY_URL = "format_avro_schema_registry_url"


class NeedMoreError(Exception):
    """Буфер кончился раньше конца заголовка."""


class AvroCursor:
    """Чтение примитивов Avro из буфера: zigzag-varint long и bytes."""

    def __init__(self, buffer: bytes) -> None:
        self._buffer = buffer
        self.position = 0

    def long(self) -> int:
        shift = 0
        result = 0
        while True:
            if self.position >= len(self._buffer):
                raise NeedMoreError

            byte = self._buffer[self.position]
            self.position += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break

            shift += 7

        return (result >> 1) ^ -(result & 1)

    def raw(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self._buffer):
            raise NeedMoreError

        chunk = self._buffer[self.position : end]
        self.position = end

        return chunk

    def sized(self) -> bytes:
        return self.raw(self.long())


class ContainerHead:
    """Снимает заголовок контейнера с начала байтового потока: магия, карта
    метаданных блоками (счётчик, при отрицательном ещё размер, пары
    ключ-значение) до нулевого счётчика, маркер синхронизации."""

    MAGIC: ClassVar[bytes] = b"Obj\x01"
    SYNC_SIZE: ClassVar[int] = 16
    ENCODING: ClassVar[str] = "utf-8"
    DEFAULT_CODEC: ClassVar[str] = "null"

    def __init__(self, fmt: str) -> None:
        self._fmt = fmt

    async def take(self, chunks: AsyncIterator[memoryview]) -> AvroColumns:
        buffer = bytearray()
        while True:
            parsed = self._parsed(bytes(buffer), chunks)
            if parsed is not None:
                return parsed

            chunk = await anext(chunks, None)
            if chunk is None:
                raise ClickHouseFormatError(
                    f"reading {self._fmt} header: expected the container header, "
                    f"the stream ended after {len(buffer)} bytes"
                )

            buffer.extend(chunk)

    def _parsed(
        self, buffer: bytes, chunks: AsyncIterator[memoryview]
    ) -> AvroColumns | None:
        head = buffer[: len(self.MAGIC)]
        if len(head) < len(self.MAGIC) and self.MAGIC.startswith(head):
            return None

        if head != self.MAGIC:
            raise ClickHouseFormatError(
                f"reading {self._fmt} header: expected magic {self.MAGIC!r}, "
                f"got {head!r}"
            )

        cursor = AvroCursor(buffer)
        cursor.position = len(self.MAGIC)
        try:
            metadata = self._metadata(cursor)
            cursor.raw(self.SYNC_SIZE)
        except NeedMoreError:
            return None

        schema = self._schema(metadata)
        codec = metadata.get(AvroMetadata.CODEC, self.DEFAULT_CODEC.encode())

        names: list[str] = []
        for field in schema["fields"]:
            names.append(field["name"])

        return AvroColumns(
            names=tuple(names),
            schema=schema,
            codec=codec.decode(self.ENCODING),
            head=buffer[: cursor.position],
            blocks=Lines.all(buffer[cursor.position :], chunks),
        )

    def _metadata(self, cursor: AvroCursor) -> dict[str, bytes]:
        metadata: dict[str, bytes] = {}
        while True:
            count = cursor.long()
            if count == 0:
                return metadata

            if count < 0:
                count = -count
                cursor.long()

            for _ in range(count):
                key = cursor.sized().decode(self.ENCODING)
                metadata[key] = cursor.sized()

    def _schema(self, metadata: Mapping[str, bytes]) -> dict[str, Any]:
        raw = metadata.get(AvroMetadata.SCHEMA)
        if raw is None:
            listed = ", ".join(sorted(metadata))
            raise ClickHouseFormatError(
                f"reading {self._fmt} header: expected {AvroMetadata.SCHEMA} in "
                f"the container metadata, got keys {listed or 'none'}"
            )

        try:
            schema = parse_schema(json.loads(raw))
        except (ValueError, SchemaParseException) as exc:
            raise ClickHouseFormatError(
                f"reading {self._fmt} header: expected an Avro schema, got "
                f"{raw[:200]!r}: {exc}"
            ) from exc

        if not isinstance(schema, dict) or schema.get("type") != "record":
            raise ClickHouseFormatError(
                f"reading {self._fmt} header: expected a record schema, got "
                f"{raw[:200]!r}"
            )

        return schema


class Avro(StreamFormat[AvroColumns]):
    """Реализация StreamFormat для контейнера Avro: read снимает заголовок и
    отдаёт блоки записей как есть, write ставит заголовок обратно. Сжатие
    блоков задаёт сервер (по умолчанию snappy, output_format_avro_codec);
    Decimal старые серверы в Avro не выводят."""

    FORMAT: ClassVar[str] = "Avro"

    def __init__(self) -> None:
        self._head = ContainerHead(self.FORMAT)
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> AvroColumns:
        return await self._head.take(Lines.views(blocks))

    def write(self, stream: AvroColumns) -> AsyncIterator[memoryview]:
        return Lines.all(stream.head, stream.blocks)


class AvroConfluent(StreamFormat[AvroConfluentStream]):
    """Реализация StreamFormat для AvroConfluent: сообщения идут как есть, а
    адрес реестра схем уходит серверу настройкой и на вход, и на выход.
    Вывод есть только у новых серверов и требует ещё
    output_format_avro_confluent_subject через extra."""

    FORMAT: ClassVar[str] = "AvroConfluent"

    def __init__(self, registry_url: str) -> None:
        if not registry_url:
            raise ClickHouseFormatError(
                f"{self.FORMAT}: expected the schema registry url, got none"
            )

        registry = {AvroSetting.REGISTRY_URL.value: registry_url}
        self._output = Settings(registry)
        self._input = Settings(registry)

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> AvroConfluentStream:
        return AvroConfluentStream(blocks=Lines.views(blocks))

    def write(self, stream: AvroConfluentStream) -> AsyncIterator[memoryview]:
        return stream.blocks
