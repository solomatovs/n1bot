"""Тело вставки Arrow без маркера конца потока: при любой нарезке кадров
серверу уходит тот же поток без последних восьми байт маркера.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Sequence

import pyarrow
import pytest

from boba.db.clickhouse.arrow_stream import ArrowBodyWithoutEos
from boba.db.clickhouse.formats.arrow import ArrowEos
from boba.toolkit.stream import Chunk

pytestmark = pytest.mark.anyio


def _stream(rows: int) -> bytes:
    table = pyarrow.table({"x": pyarrow.array(range(rows), pyarrow.int64())})
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)

    return sink.getvalue()


async def _shaped(frames: Sequence[Chunk]) -> bytes:
    async def blocks() -> AsyncIterator[Chunk]:
        for frame in frames:
            yield frame

    sent = bytearray()
    async for block in ArrowBodyWithoutEos().shaped(blocks()):
        if not len(block):
            raise AssertionError("an empty block would end a chunked request body")

        sent.extend(block)

    return bytes(sent)


def _cut(stream: bytes, size: int) -> list[bytes]:
    return [stream[start : start + size] for start in range(0, len(stream), size)]


@pytest.mark.parametrize("size", [1, 3, 7, 8, 9, 64, 4096, 1 << 20])
async def test_marker_is_dropped_however_the_stream_is_cut(size: int) -> None:
    stream = _stream(1000)
    if not stream.endswith(ArrowEos.MARKER):
        raise AssertionError("pyarrow ends a stream with the marker")

    sent = await _shaped(_cut(stream, size))

    if sent != stream[: -len(ArrowEos.MARKER)]:
        raise AssertionError(
            f"frames of {size} bytes: the body is the stream less the marker"
        )


async def test_marker_in_its_own_frame_is_dropped() -> None:
    stream = _stream(10)

    sent = await _shaped([memoryview(stream[:-8]), stream[-8:]])

    if sent != stream[:-8]:
        raise AssertionError("a frame holding only the marker is not sent")


async def test_stream_without_the_marker_is_sent_whole() -> None:
    stream = _stream(10)[:-8]

    sent = await _shaped(_cut(stream, 50))

    if sent != stream:
        raise AssertionError("a stream that ends without the marker loses nothing")


async def test_body_without_the_marker_is_still_a_readable_stream() -> None:
    sent = await _shaped(_cut(_stream(1000), 100))

    table = pyarrow.ipc.open_stream(io.BytesIO(sent)).read_all()

    if table.num_rows != 1000:
        raise AssertionError(
            f"the reader takes the end of the body as the end: {table}"
        )
