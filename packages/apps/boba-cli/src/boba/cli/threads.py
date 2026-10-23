"""Потоки процесса агента: у каждой сессии и у хранилища истории — свой
поток ОС со своим циклом событий; соединение с клиентом живёт в главном
цикле процесса.

Ресурсы, привязанные к циклу (пул Postgres, клиенты http моделей,
соединение SDK), между циклами не делятся: в чужой цикл уходит корутина
целиком (LoopCall), а хранилище истории сессии зовут через посредника
ThreadedHistoryStore, который исполняет каждый вызов в цикле хранилища.
Тяжёлый участок одной сессии вытесняется GIL и не останавливает цикл
других сессий и соединения.

Ошибки:
SessionLimitError — открыто столько сессий, сколько разрешено процессу.
Остальное — ошибки исполняемых корутин, как есть.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from itertools import count
from typing import Any, ClassVar, TypeVar

from boba.agent.history import (
    HistoryLine,
    HistoryStore,
    SessionId,
    SessionLease,
    StoredPage,
)
from boba.agent.records import Record

__all__ = [
    "LoopCall",
    "LoopThread",
    "SessionLimitError",
    "SessionThreads",
    "ThreadedHistoryStore",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SessionLimitError(Exception):
    """Процесс держит столько открытых сессий, сколько ему разрешено."""


class LoopCall:
    """Исполнение корутины в цикле loop из любого потока: из своего цикла —
    прямо, из чужого — run_coroutine_threadsafe с ожиданием итога; отмена
    ожидающего отменяет и исполнение. Держат его поток (LoopThread) и связь
    агента с клиентом (ClientLink) для главного цикла."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    async def run(self, work: Coroutine[Any, Any, T]) -> T:
        if asyncio.get_running_loop() is self._loop:
            return await work

        future = asyncio.run_coroutine_threadsafe(work, self._loop)

        return await asyncio.wrap_future(future)

    def soon(self, action: Callable[[], None]) -> None:
        """Синхронное действие в цикле loop, без ожидания."""
        self._loop.call_soon_threadsafe(action)


class LoopThread:
    """Поток ОС с собственным циклом событий: сессия агента или хранилище.

    Создают SessionThreads и вход процесса (поток хранилища). start ждёт,
    пока цикл заработает; stop отменяет задачи цикла, закрывает асинхронные
    генераторы и ждёт конца потока.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._loop = asyncio.new_event_loop()
        self._call = LoopCall(self._loop)
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)

    @property
    def name(self) -> str:
        return self._name

    @property
    def call(self) -> LoopCall:
        return self._call

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()

    async def run(self, work: Coroutine[Any, Any, T]) -> T:
        return await self._call.run(work)

    def soon(self, action: Callable[[], None]) -> None:
        self._call.soon(action)

    async def stop(self) -> None:
        """Задачи цикла отменены, цикл остановлен, поток закончился."""
        if not self._thread.is_alive():
            return

        await self.run(self._cancel_tasks())
        self._loop.call_soon_threadsafe(self._loop.stop)
        await asyncio.to_thread(self._thread.join)

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _cancel_tasks(self) -> None:
        current = asyncio.current_task()
        pending: list[asyncio.Task[Any]] = []
        for task in asyncio.all_tasks():
            if task is current:
                continue

            task.cancel()
            pending.append(task)

        outcomes = await asyncio.gather(*pending, return_exceptions=True)
        for task, outcome in zip(pending, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                continue

            if isinstance(outcome, BaseException):
                logger.error(
                    "thread %s: task %s ended with an error while the thread "
                    "stopped: %s",
                    self._name,
                    task.get_name(),
                    outcome,
                )


class SessionThreads:
    """Потоки сессий процесса с пределом их числа.

    Создаёт вход ACP (AcpAgent): каждая открытая сессия получает свой поток
    под своим именем; сверх предела открытие отказывает.
    """

    PREFIX: ClassVar[str] = "acp-session-"

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._open: dict[str, LoopThread] = {}
        self._numbers = count(1)

    def open(self, session_id: str) -> LoopThread:
        """Новый запущенный поток сессии.

        Ошибки:
        SessionLimitError — открыто limit сессий.
        """
        if len(self._open) >= self._limit:
            msg = (
                f"session {session_id} cannot be opened: this agent process already "
                f"serves {len(self._open)} session(s), its limit is {self._limit}; "
                "close a session or raise sessions.maxOpen in the settings"
            )
            raise SessionLimitError(msg)

        thread = LoopThread(f"{self.PREFIX}{next(self._numbers)}")
        thread.start()
        self._open[session_id] = thread

        return thread

    async def stop(self, session_id: str) -> None:
        thread = self._open.pop(session_id, None)
        if thread is None:
            return

        await thread.stop()

    def opened(self) -> Sequence[str]:
        return list(self._open)


class ThreadedHistoryStore(HistoryStore):
    """Реализация HistoryStore посредником: каждый вызов исполняется в цикле
    потока хранилища, где живут его пул и соединение замков; чтение журнала
    приходит порциями по READ_CHUNK строк за переход между потоками.

    Создаёт вход процесса над настоящим хранилищем и его потоком; зовут его
    агенты ядра сессий из своих циклов.
    """

    READ_CHUNK: ClassVar[int] = 64

    def __init__(self, store: HistoryStore, thread: LoopThread) -> None:
        self._store = store
        self._thread = thread

    async def acquire(self, session: SessionId) -> SessionLease:
        return await self._thread.run(self._store.acquire(session))

    async def append(self, lease: SessionLease, records: Sequence[Record]) -> None:
        await self._thread.run(self._store.append(lease, records))

    async def read(self, session: SessionId) -> AsyncGenerator[HistoryLine, None]:
        lines = self._store.read(session)
        try:
            while True:
                chunk = await self._thread.run(self._chunk(lines))
                if not chunk:
                    return

                for line in chunk:
                    yield line
        finally:
            await self._thread.run(lines.aclose())

    async def release(self, lease: SessionLease) -> None:
        await self._thread.run(self._store.release(lease))

    async def close(self) -> None:
        await self._thread.run(self._store.close())

    async def list(self, cursor: str | None, limit: int) -> StoredPage:
        return await self._thread.run(self._store.list(cursor, limit))

    async def delete(self, session: SessionId) -> None:
        await self._thread.run(self._store.delete(session))

    async def copy(
        self, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        return await self._thread.run(self._store.copy(source, target, up_to))

    async def _chunk(
        self, lines: AsyncGenerator[HistoryLine, None]
    ) -> list[HistoryLine]:
        chunk: list[HistoryLine] = []
        async for line in lines:
            chunk.append(line)
            if len(chunk) >= self.READ_CHUNK:
                break

        return chunk
