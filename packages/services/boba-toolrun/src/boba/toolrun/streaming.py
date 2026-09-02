"""Потоковый вызов инструмента из asyncio: кадры между loop'ом и насосом.

ToolCall блокирует поток: frames ждёт следующего кадра, result — конца
вызова. Здесь этот порт превращается в асинхронный — чтение кадров живёт в
рабочем потоке и складывает их в очередь loop'а, отправка и конверт уезжают
в to_thread.

Ошибки:
LauncherError — вызов нарушил контракт исполнителя; поднимается там, где
    её поднял бы сам ToolCall: на кадре либо на конверте.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator

from boba.toolkit.frames import ToolFrame
from boba.toolkit.launcher import ToolCall, ToolLauncher, ToolOutcome
from boba.toolkit.protocol import ToolCommand

__all__ = ["AsyncToolCall"]


class AsyncToolCall:
    """Асинхронная обёртка над ToolCall для кода из event loop'а.

    ToolCall блокирует поток (frames ждёт кадра, send может стоять на
    полном пайпе), поэтому напрямую из asyncio им пользоваться нельзя.
    Здесь чтение кадров живёт в отдельном потоке и складывает их в очередь
    loop'а через call_soon_threadsafe, а send/done_sending/result уезжают в
    to_thread. Ошибка чтения приезжает тем же путём и поднимается у
    потребителя.
    """

    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[ToolFrame | None] = asyncio.Queue()
        self._failure: BaseException | None = None

        self._reader = threading.Thread(
            target=self._read_frames,
            name="tool-call-frames",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def opened(cls, launcher: ToolLauncher, command: ToolCommand) -> AsyncToolCall:
        return cls(launcher.open(command))

    async def send(self, frame: ToolFrame) -> None:
        await asyncio.to_thread(self._call.send, frame)

    async def done_sending(self) -> None:
        await asyncio.to_thread(self._call.done_sending)

    async def frames(self) -> AsyncIterator[ToolFrame]:
        while True:
            frame = await self._queue.get()
            if frame is None:
                if self._failure is not None:
                    raise self._failure

                return

            yield frame

    async def result(self) -> ToolOutcome:
        return await asyncio.to_thread(self._call.result)

    async def close(self) -> None:
        await asyncio.to_thread(self._call.close)

    def _read_frames(self) -> None:
        try:
            for frame in self._call.frames():
                self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)
        except BaseException as exc:
            self._failure = exc
        finally:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
