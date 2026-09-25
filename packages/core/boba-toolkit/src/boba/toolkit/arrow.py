"""Порты потока Arrow IPC поверх сырых: тело насоса получает и отдаёт пачки
записей, а не байты. На проводе — обычный поток IPC (схема, пачки, конец),
поэтому такой порт стыкуется с любым сырым концом, который пишет или
читает Arrow IPC (ClickHouse с FORMAT ArrowStream, ora_arrow_*).

pyarrow импортируется в момент открытия порта: модуль читает и хост ради
объявлений инструментов, а pyarrow живёт только в песочнице плагина. По той
же причине объекты pyarrow в подписях не типизированы.

Ошибки:
ArrowStreamError — байты входа не читаются как поток Arrow IPC или
    оборвались посреди пачки.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from boba.toolkit.ports import RawInbound, RawOutbound

__all__ = [
    "ArrowInbound",
    "ArrowOutbound",
    "ArrowReader",
    "ArrowStreamError",
    "ArrowWriter",
]


class ArrowStreamError(Exception):
    """Входной поток не читается как Arrow IPC."""


@dataclass(frozen=True)
class ArrowReader:
    """Открытый входной поток: схема из его начала и пачки по мере чтения."""

    schema: Any
    batches: AsyncIterator[Any]


class ArrowWriter:
    """Открытый выходной поток: пачки и таблицы уходят в порт по мере записи,
    close пишет конец потока. Запись блокирующая и идёт в потоке."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    async def write(self, batch: Any) -> None:
        """Пачка записей или таблица той же схемы."""
        await asyncio.to_thread(self._writer.write, batch)

    async def close(self) -> None:
        await asyncio.to_thread(self._writer.close)


class ArrowInbound(RawInbound):
    """Входной порт потока Arrow IPC: open читает схему, дальше пачки идут по
    мере итерации. Поверх сырого порта стоит io.BufferedReader с одним
    переиспользуемым буфером buffer_bytes — читатель IPC ждёт от read(n)
    ровно n байт. Чтение блокирующее и идёт в потоке."""

    async def open(self, buffer_bytes: int) -> ArrowReader:
        import pyarrow  # noqa: PLC0415
        import pyarrow.ipc  # noqa: PLC0415

        buffered = io.BufferedReader(self, buffer_bytes)
        try:
            reader = await asyncio.to_thread(pyarrow.ipc.open_stream, buffered)
        except pyarrow.ArrowException as exc:
            raise ArrowStreamError(
                f"reading an arrow ipc stream schema failed: {type(exc).__name__}: "
                f"{exc}"
            ) from exc

        return ArrowReader(schema=reader.schema, batches=self._batches(reader))

    async def _batches(self, reader: Any) -> AsyncIterator[Any]:
        import pyarrow  # noqa: PLC0415

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
    def _next(reader: Any) -> Any:
        try:
            return reader.read_next_batch()
        except StopIteration:
            return None


class ArrowOutbound(RawOutbound):
    """Выходной порт потока Arrow IPC: open пишет схему и отдаёт писателя,
    пачки уходят в провод по мере записи, close — конец потока."""

    async def open(self, schema: Any) -> ArrowWriter:
        import pyarrow.ipc  # noqa: PLC0415

        writer = await asyncio.to_thread(pyarrow.ipc.new_stream, self, schema)

        return ArrowWriter(writer)
