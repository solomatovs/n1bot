"""Мост из async-источника байтов в синхронный ридер: чанки корутины уходят в
пипу, потребитель читает её конец файловым объектом в рабочем потоке. Так
тело http-ответа попадает в DocumentRouter без файла на диске и без буфера
целиком у вызывающего.

Ошибки:
Ошибка источника (async-итератора) и ошибка потребителя выходят как есть:
    источник главнее, ошибка потребителя при этом добавляется заметкой.
"""

from __future__ import annotations

import asyncio
import os
from asyncio.streams import FlowControlMixin
from collections.abc import AsyncIterator, Callable
from typing import BinaryIO, TypeVar

__all__ = ["AsyncPipe"]

T = TypeVar("T")


class AsyncPipe:
    """Перекачка async-чанков в синхронного потребителя через os.pipe.

    Писатель — StreamWriter на записывающем конце пипы, поэтому цикл событий
    не блокируется: drain ждёт, пока потребитель освободит место. Потребитель
    получает BinaryIO читающего конца и сам закрывает его по завершении.
    """

    @classmethod
    async def run(
        cls, chunks: AsyncIterator[bytes], consume: Callable[[BinaryIO], T]
    ) -> T:
        loop = asyncio.get_running_loop()
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, "rb")
        consumer = loop.run_in_executor(None, cls._consume, consume, source)

        sink = os.fdopen(write_fd, "wb", buffering=0)
        transport, protocol = await loop.connect_write_pipe(FlowControlMixin, sink)
        writer = asyncio.StreamWriter(transport, protocol, None, loop)

        try:
            await cls._pump(chunks, writer)
        except Exception as exc:
            writer.close()
            await cls._settle(consumer, exc)
            raise
        finally:
            writer.close()

        return await consumer

    @staticmethod
    async def _pump(chunks: AsyncIterator[bytes], writer: asyncio.StreamWriter) -> None:
        """Потребитель вправе закрыть свой конец раньше конца данных: пипа
        рвётся, и это не ошибка перекачки — итог решает сам потребитель."""
        try:
            async for chunk in chunks:
                writer.write(chunk)
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            return

    @staticmethod
    def _consume(consume: Callable[[BinaryIO], T], source: BinaryIO) -> T:
        with source:
            return consume(source)

    @staticmethod
    async def _settle(consumer: asyncio.Future[T], cause: Exception) -> None:
        """Источник упал: дождаться потребителя, его ошибку приложить заметкой."""
        try:
            await consumer
        except Exception as exc:
            cause.add_note(f"reader failed on the truncated stream too: {exc}")
