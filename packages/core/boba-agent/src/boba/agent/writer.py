"""Писатель журнала: переносит несброшенные записи сессии в порт истории.

Задача в цикле событий просыпается по событию «добавлена запись» и не реже
раза в interval_sec, берёт записи с seq больше последнего сброшенного и зовёт
`append` одной пачкой (план, раздел 5.15). Неудача — запись в лог, записи
остаются несброшенными, следующий тик пробует снова; ход не останавливается.
Принудительный сброс с ожиданием — в конце хода и при завершении процесса.

RecordKeeper — единственная дверь, через которую ход и исполнитель
инструментов добавляют записи: запись уходит в сессию, писатель просыпается,
записи разговора и граница сжатия показываются наружу событием.

Ошибки:
HistoryError — поднимает только `flush(strict=True)` при завершении, когда
    журнал так и не удалось сбросить; фоновая задача ошибки не выпускает.
SessionError — нарушен порядок записей сессии (ошибка ядра).
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar, TypeVar

from boba.agent.events import AgentEvent, EventSink, RecordEvent
from boba.agent.history import HistoryError, HistoryStore, SessionLease
from boba.agent.records import (
    AssistantRecord,
    CompactBoundaryRecord,
    Record,
    UserRecord,
)
from boba.agent.session import Session

logger = logging.getLogger(__name__)

__all__ = ["HistoryWriter", "RecordKeeper"]

R = TypeVar("R", bound=Record)


class HistoryWriter:
    """Фоновый сброс записей сессии в хранилище истории."""

    DEFAULT_INTERVAL_SEC: ClassVar[float] = 0.1

    def __init__(
        self,
        session: Session,
        store: HistoryStore,
        lease: SessionLease,
        flushed_seq: int,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
    ) -> None:
        self._session = session
        self._store = store
        self._lease = lease
        self._flushed = flushed_seq
        self._interval = interval_sec
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._last_error = ""

    @property
    def flushed_seq(self) -> int:
        return self._flushed

    @property
    def unflushed(self) -> int:
        return len(self._session.since(self._flushed))

    @property
    def last_error(self) -> str:
        """Текст последней неудачи сброса; пусто — сбросы проходят."""
        return self._last_error

    def start(self) -> None:
        if self._task is not None:
            return

        self._task = asyncio.create_task(self._run(), name="history-writer")

    def notify(self) -> None:
        """Добавлена запись: писатель просыпается, не дожидаясь тика."""
        self._wake.set()

    async def flush(self, strict: bool = False) -> bool:
        """Сбрасывает всё несброшенное сейчас; False — хранилище отказало."""
        try:
            await self._append_pending()
        except HistoryError as exc:
            self._last_error = str(exc)
            logger.error(
                "history flush failed, %d record(s) kept: %s", self.unflushed, exc
            )
            if strict:
                raise

            return False

        self._last_error = ""

        return True

    async def stop(self) -> None:
        """Последняя попытка сброса и остановка задачи."""
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

        await self.flush()

    def failed(self) -> BaseException | None:
        """Исключение, которым упала задача писателя; None — живёт."""
        if self._task is None or not self._task.done():
            return None

        if self._task.cancelled():
            return None

        return self._task.exception()

    async def _run(self) -> None:
        while not self._stopping:
            await self._sleep_until_wake()
            await self.flush()

    async def _sleep_until_wake(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
        except TimeoutError:
            return

        self._wake.clear()

    async def _append_pending(self) -> None:
        """Один сброс за раз: тик и принудительный flush не пишут одно и то же."""
        async with self._lock:
            pending = self._session.since(self._flushed)
            if not pending:
                return

            await self._store.append(self._lease, pending)
            self._flushed = pending[-1].seq


class RecordKeeper:
    """Добавляет записи в сессию, будит писателя и показывает наружу записи
    разговора. Создаёт агент на сессию; им пользуются цикл хода и
    исполнитель инструментов."""

    def __init__(
        self, session: Session, writer: HistoryWriter, sink: EventSink
    ) -> None:
        self._session = session
        self._writer = writer
        self._sink = sink

    @property
    def session(self) -> Session:
        return self._session

    async def add(self, record: R) -> R:
        """Запись без seq: сессия выдаёт seq, писатель и порт узнают о ней."""
        sequenced = self._session.add(record)
        await self.added(sequenced)

        return sequenced

    async def added(self, record: Record) -> None:
        """Запись, уже добавленная в сессию кем-то другим."""
        self._writer.notify()
        if isinstance(record, UserRecord | AssistantRecord | CompactBoundaryRecord):
            await self._sink.emit(RecordEvent(record=record))

    async def emit(self, event: AgentEvent) -> None:
        await self._sink.emit(event)
