"""Чтение и запись потока Arrow IPC через порты ArrowInbound и ArrowOutbound:
тело насоса получает и отдаёт пачки записей, а не байты. Модуль тянет
pyarrow (extra `arrow`), поэтому импортируется телом инструмента при вызове,
а не модулем объявлений.

Ошибки:
ArrowStreamError — байты входа не читаются как поток Arrow IPC или
    оборвались посреди пачки.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pyarrow
import pyarrow.ipc

from boba.toolkit.ports import ArrowInbound, ArrowOutbound, ArrowStreamError

__all__ = ["ArrowIpc", "ArrowReader", "ArrowWriter"]


@dataclass(frozen=True)
class ArrowReader:
    """Открытый входной поток: схема из его начала и пачки по мере чтения."""

    schema: pyarrow.Schema
    batches: AsyncIterator[pyarrow.RecordBatch]


class ArrowWriter:
    """Открытый выходной поток: пачки и таблицы уходят в порт по мере записи,
    close пишет конец потока. Запись блокирующая и идёт в потоке."""

    def __init__(self, writer: pyarrow.ipc.RecordBatchStreamWriter) -> None:
        self._writer = writer

    async def write(self, batch: pyarrow.RecordBatch | pyarrow.Table) -> None:
        """Пачка записей или таблица той же схемы."""
        await asyncio.to_thread(self._writer.write, batch)

    async def close(self) -> None:
        await asyncio.to_thread(self._writer.close)


class ArrowIpc:
    """Открывает потоки IPC над портами. На входе поверх сырого порта стоит
    io.BufferedReader с одним переиспользуемым буфером buffer_bytes: читатель
    IPC ждёт от read(n) ровно n байт, а сырой порт отдаёт короткие чтения.
    На выходе писатель IPC пишет в порт напрямую. Чтение и запись
    блокирующие и идут в потоке."""

    async def open_in(self, port: ArrowInbound, buffer_bytes: int) -> ArrowReader:
        buffered = io.BufferedReader(port, buffer_bytes)
        try:
            reader = await asyncio.to_thread(pyarrow.ipc.open_stream, buffered)
        except pyarrow.ArrowException as exc:
            raise ArrowStreamError(
                f"reading an arrow ipc stream schema failed: {type(exc).__name__}: "
                f"{exc}"
            ) from exc

        return ArrowReader(schema=reader.schema, batches=self._batches(reader))

    async def open_out(
        self, port: ArrowOutbound, schema: pyarrow.Schema
    ) -> ArrowWriter:
        writer = await asyncio.to_thread(pyarrow.ipc.new_stream, port, schema)

        return ArrowWriter(writer)

    async def _batches(
        self, reader: pyarrow.ipc.RecordBatchStreamReader
    ) -> AsyncIterator[pyarrow.RecordBatch]:
        while True:
            try:
                batch = await asyncio.to_thread(self._next, reader)
            except pyarrow.ArrowException as exc:
                raise ArrowStreamError(
                    f"reading an arrow ipc batch failed: {type(exc).__name__}: {exc}"
                ) from exc

            if batch is None:
                return

            yield batch

    @staticmethod
    def _next(
        reader: pyarrow.ipc.RecordBatchStreamReader,
    ) -> pyarrow.RecordBatch | None:
        try:
            return reader.read_next_batch()
        except StopIteration:
            return None
