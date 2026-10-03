"""Потоки живого вывода вызовов инструментов: запись в журнал, окна для
панели, живые журналы запусков.

Ошибки:
StreamJournalError — журнал недоступен или окно нарушает контракт.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from typing import ClassVar

from pydantic import ValidationError

from boba.canvas.canvas import (
    WatchProbe,
    WatchSource,
)
from boba.canvas.journal import (
    CallStream,
    ChannelProbe,
    StreamJournalError,
    StreamKey,
    StreamRecorderPort,
    StreamSlice,
    StreamStorePort,
)
from boba.identity.run import Run, Runs
from boba.messaging import StreamAppended, StreamFeed
from boba.toolkit.channels import (
    CallOutcome,
    JournalChannel,
    JournalChannels,
    ToolChannel,
)
from boba.toolkit.frames import (
    FrameCodec,
    FrameLimit,
    FrameProtocolError,
    ToolFrame,
)
from boba.toolkit.stream import ChannelSinks, Chunk, StreamSink

__all__ = [
    "CallJournals",
    "FrameHeadsSink",
    "JournalWatchSource",
    "StreamPump",
    "StreamPumps",
    "ToolStream",
]

logger = logging.getLogger(__name__)


class ToolStream(ChannelSinks, CallStream):
    """Журнал одного живого вызова инструмента: рекордер на каждый канал
    плюс будильники для слежения из event loop'а. Реализует ChannelSinks
    (его получает исполнитель через ToolChannelsTap) и CallStream (чтение
    панелью); создаёт и держит его CallJournals.

    Создаётся в потоке исполнения инструмента; свой event loop стрим не
    запоминает. Будильники подключают слежение и насос шины из loop'а
    приложения; вывод, пришедший до подключения, уже лежит в журнале. Канал
    stdout открывается сразу — панель находит файл до первого байта;
    остальные — по обращению.
    """

    def __init__(
        self,
        key: StreamKey,
        tool_name: str,
        journal: StreamStorePort,
        protected_prefixes: Callable[[], frozenset[str]],
    ) -> None:
        self._key = key
        self._tool_name = tool_name
        self._journal = journal
        self._protected = protected_prefixes
        """Префиксы живых вызовов процесса на момент открытия канала."""
        self._lock = threading.Lock()
        self._wakers: dict[asyncio.Event, asyncio.AbstractEventLoop] = {}
        self._recorders: dict[JournalChannel, StreamRecorderPort] = {}
        self._open(ToolChannel.STDOUT)

    @property
    def key(self) -> StreamKey:
        return self._key

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def call_prefix(self) -> str:
        """Префикс файлов вызова: единица защиты от ротации журнала."""
        return self._key.call_prefix()

    @property
    def closed(self) -> bool:
        with self._lock:
            recorders = list(self._recorders.values())

        return all(recorder.closed for recorder in recorders)

    def sink_of(self, channel: JournalChannel) -> StreamSink:
        """Приёмник канала; рекордер открывается при первом обращении.

        Кадровые каналы (вход stdin и кадры наружу) журналируются одними
        заголовками: тела кадров бинарны и растут как поток данных (аудио,
        файлы), в разборе сбоев от них толку нет.
        """
        recorder = self._open(channel)
        if channel not in (ToolChannel.FRAMES, ToolChannel.STDIN):
            return recorder

        return FrameHeadsSink(recorder)

    def close(self, note: str) -> None:
        """Закрыть все каналы вызова одной пометкой и разбудить слежение; повтор
        безвреден.
        """
        with self._lock:
            recorders = list(self._recorders.values())

        for recorder in recorders:
            recorder.close(note)

        self._wake()

    def probe(self, channel: JournalChannel) -> WatchProbe:
        """Состояние канала без чтения файла: размер и итог рекордера."""
        recorder = self._open(channel)
        size = recorder.size
        closed = recorder.closed

        return WatchProbe(
            revision=f"{size}:{int(closed)}",
            size=size,
            closed=closed,
            final=closed,
            note=recorder.note,
        )

    def probes(self) -> Sequence[ChannelProbe]:
        """Состояние всех открытых каналов вызова."""
        with self._lock:
            channels = list(self._recorders)

        found: list[ChannelProbe] = []
        for channel in channels:
            found.append(ChannelProbe(channel=channel, probe=self.probe(channel)))

        return found

    def _open(self, channel: JournalChannel) -> StreamRecorderPort:
        with self._lock:
            recorder = self._recorders.get(channel)
            if recorder is not None:
                return recorder

            protected = self._protected() | {self._key.call_prefix()}
            recorder = self._journal.recorder(
                self._key, self._tool_name, channel, self._wake, protected
            )
            self._recorders[channel] = recorder
            return recorder

    def attach_waker(self) -> asyncio.Event:
        """Событие пробуждения в текущем loop'е; каждая запись и закрытие поднимают
        его.
        """
        event = asyncio.Event()
        loop = asyncio.get_running_loop()

        with self._lock:
            self._wakers[event] = loop

        return event

    def detach_waker(self, event: asyncio.Event) -> None:
        """Снимает будильник, выданный attach_waker."""
        with self._lock:
            self._wakers.pop(event, None)

    def _wake(self) -> None:
        with self._lock:
            wakers = list(self._wakers.items())

        for event, loop in wakers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                logger.debug("stream wakeup after loop shutdown: %s", self._key.call_id)


class FrameHeadsSink(StreamSink):
    """Реализация StreamSink для журнала канала кадров: пишет строку
    заголовка и размер тела вместо самого тела.

    Тела кадров — бинарный поток данных (аудио, файлы): писать их в журнал
    значило бы дублировать гигабайты, а для разбора сбоев хватает
    заголовков. Кадр, который не разбирается, отмечается строкой отказа —
    канал ведёт сам лончер, и молчать о нарушении протокола здесь нельзя.
    Ставится поверх рекордера канала в ToolStream.sink_of.
    """

    def __init__(self, recorder: StreamSink) -> None:
        self._recorder = recorder
        self._codec = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)

    def feed(self, data: Chunk) -> None:
        try:
            frames = self._codec.feed(data)
        except FrameProtocolError as exc:
            self._recorder.feed_text(f"frame stream broken: {exc}\n")
            return

        for frame in frames:
            self._recorder.feed_text(self._line(frame))

    def feed_text(self, text: str) -> None:
        self._recorder.feed_text(text)

    @staticmethod
    def _line(frame: ToolFrame) -> str:
        header = frame.header.decode("utf-8", errors="replace")
        return f"{header} +{len(frame.body)}b\n"


class StreamPump:
    """Следит за живым журналом одного вызова из loop'а запуска и сообщает ленте о
    каждом росте видимого канала; частые записи склеиваются в одно сообщение.
    """

    COALESCE_SEC: ClassVar[float] = 0.25
    """Пауза после пробуждения: болтливый инструмент не заливает шину."""

    POLL_SEC: ClassVar[float] = 1.0
    """Предел ожидания будильника: страховка от пропущенного пробуждения."""

    def __init__(self, stream: ToolStream, feed: StreamFeed) -> None:
        self._stream = stream
        self._feed = feed
        self._seen: dict[JournalChannel, str] = {}

    async def run(self) -> None:
        waker = self._stream.attach_waker()
        try:
            while True:
                closed = self._stream.closed
                await self._report()
                if closed:
                    return

                await self._pause(waker)
        finally:
            self._stream.detach_waker(waker)

    async def _report(self) -> None:
        for item in self._stream.probes():
            if not JournalChannels.visible(item.channel):
                continue

            if self._seen.get(item.channel) == item.probe.revision:
                continue

            self._seen[item.channel] = item.probe.revision
            message = StreamAppended(
                call_id=self._stream.key.call_id,
                channel=item.channel.value,
                size=item.probe.size,
                closed=item.probe.closed,
                note=item.probe.note,
            )
            await self._feed.stream_appended(message)

    async def _pause(self, waker: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(waker.wait(), timeout=self.POLL_SEC)
        except TimeoutError:
            return

        waker.clear()
        await asyncio.sleep(self.COALESCE_SEC)


class StreamPumps:
    """Заводит насос на каждый журнал, открытый запуском, и закрывает их вместе с
    запуском: сначала даёт дописать итог, потом снимает задачи.
    """

    CLOSE_SEC: ClassVar[float] = 2.0

    def __init__(self, feed: StreamFeed) -> None:
        self._feed = feed
        self._tasks: set[asyncio.Task[None]] = set()

    def opened(self, call_id: str, stream: ToolStream) -> None:
        """Наблюдатель CallJournals: журнал открыт — насос запущен в текущем loop'е."""
        pump = StreamPump(stream, self._feed)
        task = asyncio.create_task(pump.run(), name=f"stream-pump:{call_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        tasks = list(self._tasks)
        if not tasks:
            return

        done, pending = await asyncio.wait(tasks, timeout=self.CLOSE_SEC)
        for task in pending:
            task.cancel()

        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        for task in done:
            if task.cancelled():
                continue

            error = task.exception()
            if error is None:
                continue

            logger.error(
                "stream pump %s failed: %s", task.get_name(), error, exc_info=error
            )


StreamObserver = Callable[[str, ToolStream], None]
"""Узнаёт об открытом журнале вызова (call_id, stream); зовётся в loop'е запуска."""


class CallJournals:
    """Журналы живого вывода вызовов процесса: открытие, живые стримы, чтение.

    Объект один на процесс: его создаёт сборка приложения из хранилища
    журнала (None — журнал выключен, потоков нет вовсе) и реестра запусков.
    Журнал вызова открывает обвязка ToolRunLogger в потоке инструмента;
    живой стрим принадлежит запуску области и закрывается вместе с ним,
    файлы журнала переживают запуск. Панель и слой данных читают записанное
    отсюда же. Потоковыми считаются инструменты, отмеченные загрузчиком:
    только они запускают процессы, чей вывод есть смысл журналировать.
    """

    def __init__(self, store: StreamStorePort | None, runs: Runs) -> None:
        self._store = store
        self._runs = runs
        self._lock = threading.Lock()
        self._live: dict[Run, dict[str, ToolStream]] = {}
        self._followers: dict[Run, tuple[asyncio.AbstractEventLoop, StreamObserver]]
        self._followers = {}
        self._streamable: set[str] = set()

    @property
    def store(self) -> StreamStorePort | None:
        """Хранилище журнала; None — журнал выключен."""
        return self._store

    def active(self) -> bool:
        """Журнал настроен: потоки пишутся и кнопки имеют смысл."""
        return self._store is not None

    def mark_streamable(self, names: Iterable[str]) -> None:
        with self._lock:
            self._streamable.update(names)

    def streamable(self, tool_name: str) -> bool:
        if self._store is None:
            return False

        with self._lock:
            return tool_name in self._streamable

    @contextmanager
    def following(
        self, run: Run, observer: StreamObserver
    ) -> Generator[None, None, None]:
        """На время блока сообщает observer о каждом журнале, открытом запуском;
        зовёт его в loop'е, из которого открыт блок."""
        with self._lock:
            self._followers[run] = (asyncio.get_running_loop(), observer)

        try:
            yield
        finally:
            with self._lock:
                self._followers.pop(run, None)

    def begin(
        self, user_id: str, scope_id: str, call_id: str, tool_name: str
    ) -> ToolStream | None:
        """Открыть журнал вызова; сбой журнала не трогает ход инструмента."""
        store = self._store
        if store is None:
            return None

        if not self.streamable(tool_name):
            return None

        run = self._runs.active(scope_id)
        if run is None:
            logger.warning(
                "stream journal skipped call %s of %s: scope %s has no active run "
                "to attach the stream to",
                call_id,
                tool_name,
                scope_id,
            )
            return None

        try:
            key = StreamKey(user_id=user_id, thread_id=scope_id, call_id=call_id)
            stream = ToolStream(key, tool_name, store, self.live_prefixes)
        except (StreamJournalError, ValidationError) as exc:
            logger.warning(
                "stream journal refused call %s of %s in scope %s of user %s: %s",
                call_id,
                tool_name,
                scope_id,
                user_id,
                exc,
                exc_info=True,
            )
            return None

        self._attach(run, call_id, stream)
        return stream

    def live(self, scope_id: str, call_id: str) -> ToolStream | None:
        """Живой журнал вызова; None — вызов не журналируется или закончился."""
        run = self._runs.active(scope_id)
        if run is None:
            return None

        with self._lock:
            return self._live.get(run, {}).get(call_id)

    def live_prefixes(self) -> frozenset[str]:
        """Префиксы файлов живых вызовов: вытеснять их из тома нельзя."""
        with self._lock:
            return frozenset(self._live_call_prefixes())

    def recorded_slice(
        self,
        user_id: str,
        thread_id: str,
        call_id: str,
        offset: int,
        channel: JournalChannel,
    ) -> StreamSlice | None:
        """Окно журнала от смещения; отказ журнала — «нет данных», не сбой чата."""

        def read(store: StreamStorePort, key: StreamKey) -> StreamSlice | None:
            return store.slice_at(key, offset, channel)

        return self._recorded(user_id, thread_id, call_id, read)

    def recorded_slice_before(
        self,
        user_id: str,
        thread_id: str,
        call_id: str,
        end: int,
        channel: JournalChannel,
    ) -> StreamSlice | None:
        """Окно перед смещением: прокрутка вверх; отказ — «нет данных»."""

        def read(store: StreamStorePort, key: StreamKey) -> StreamSlice | None:
            return store.slice_before(key, end, channel)

        return self._recorded(user_id, thread_id, call_id, read)

    def recorded_channels(
        self, user_id: str, thread_id: str, call_id: str
    ) -> tuple[JournalChannel, ...]:
        """Каналы вызова с записью, доступные пользователю.

        Служебные каналы (конверт результата, вывод обвязки запуска) в
        панель не попадают: пишутся они всегда, читает их только разбор
        сбоев на сервере. Отказ журнала — пустой список вкладок.
        """
        store = self._store
        if store is None:
            return ()

        try:
            key = StreamKey(user_id=user_id, thread_id=thread_id, call_id=call_id)
            written = store.channels_of(key)
        except (StreamJournalError, ValidationError) as exc:
            logger.warning(
                "stream journal: listing channels of call %s in thread %s of "
                "user %s failed: %s",
                call_id,
                thread_id,
                user_id,
                exc,
                exc_info=True,
            )
            return ()

        readable: list[JournalChannel] = []
        for channel in written:
            if not JournalChannels.visible(channel):
                continue

            readable.append(channel)

        return tuple(readable)

    def _attach(self, run: Run, call_id: str, stream: ToolStream) -> None:
        """Журнал живёт с запуском: первый журнал запуска подписывает закрытие."""
        with self._lock:
            streams = self._live.get(run)
            first = streams is None
            if streams is None:
                streams = {}
                self._live[run] = streams

            streams[call_id] = stream
            follower = self._followers.get(run)

        if first:
            run.on_close(partial(self._close_run, run))

        if follower is None:
            return

        loop, observer = follower
        try:
            loop.call_soon_threadsafe(observer, call_id, stream)
        except RuntimeError as exc:
            logger.warning(
                "call journals of scope %s: stream %s opened after the run loop "
                "closed, observer not notified: %s",
                run.scope_id,
                call_id,
                exc,
            )

    def _close_run(self, run: Run) -> None:
        """Конец запуска: живые журналы закрываются, файлы журнала остаются."""
        with self._lock:
            streams = self._live.pop(run, {})

        for stream in streams.values():
            if not stream.closed:
                stream.close(CallOutcome.STOPPED.value)

    def _live_call_prefixes(self) -> Iterator[str]:
        for streams in self._live.values():
            for stream in streams.values():
                yield stream.call_prefix

    def _recorded(
        self,
        user_id: str,
        thread_id: str,
        call_id: str,
        read: Callable[[StreamStorePort, StreamKey], StreamSlice | None],
    ) -> StreamSlice | None:
        store = self._store
        if store is None:
            return None

        try:
            key = StreamKey(user_id=user_id, thread_id=thread_id, call_id=call_id)
            return read(store, key)
        except (StreamJournalError, ValidationError) as exc:
            logger.warning(
                "stream journal: reading call %s in thread %s of user %s failed: %s",
                call_id,
                thread_id,
                user_id,
                exc,
                exc_info=True,
            )
            return None


class JournalWatchSource(WatchSource):
    """Слежение за журналом вызова: живой будит записью, закрытый статичен."""

    def __init__(
        self,
        journal: StreamStorePort,
        key: StreamKey,
        channel: JournalChannel,
        live: ToolStream | None,
    ) -> None:
        self._journal = journal
        self._key = key
        self._channel = channel
        self._live = live

    async def probe(self) -> WatchProbe | None:
        if self._live is not None and not self._live.closed:
            return self._live.probe(self._channel)

        try:
            stat = self._journal.stat_of(self._key, self._channel)
        except StreamJournalError as exc:
            logger.warning(
                "stream journal: stat of %s/%s failed: %s",
                self._key.call_id,
                self._channel,
                exc,
                exc_info=True,
            )
            return None

        if stat is None:
            return None

        return WatchProbe(
            revision=f"{stat.size}:{int(stat.closed)}",
            size=stat.size,
            closed=stat.closed,
            final=stat.closed,
            note=stat.note,
        )

    def attach_waker(self) -> asyncio.Event | None:
        if self._live is None:
            return None

        return self._live.attach_waker()

    def detach_waker(self, event: asyncio.Event) -> None:
        if self._live is None:
            return

        self._live.detach_waker(event)
