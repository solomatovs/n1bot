"""Потоки живого вывода вызовов инструментов: запись в журнал, окна для
панели, реестр потоков по областям запусков.

Ошибки:
StreamJournalError — журнал недоступен или окно нарушает контракт.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
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
    StreamJournalHub,
    StreamKey,
    StreamRecorderPort,
    StreamSlice,
    StreamStorePort,
)
from boba.identity.run import LiveStream, RunRegistry
from boba.messaging import StreamAppended, StreamFeed
from boba.toolkit.channels import JournalChannel, JournalChannels, ToolChannel
from boba.toolkit.frames import (
    FrameCodec,
    FrameLimit,
    FrameProtocolError,
    ToolFrame,
)
from boba.toolkit.stream import ChannelSinks, Chunk, StreamSink

__all__ = [
    "FrameHeadsSink",
    "JournalWatchSource",
    "StreamPump",
    "StreamPumps",
    "ToolStream",
    "ToolStreams",
]

logger = logging.getLogger(__name__)


class ToolStream(ChannelSinks, CallStream, LiveStream):
    """Живой вызов инструмента: рекордеры каналов плюс будильники слежения.

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
        protected_prefixes: frozenset[str],
    ) -> None:
        self._key = key
        self._tool_name = tool_name
        self._journal = journal
        self._protected = protected_prefixes | {key.call_prefix()}
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

        Канал кадров журналируется заголовками: тела кадров бинарны и растут
        как поток данных (аудио, файлы), в разборе сбоев от них толку нет.
        """
        recorder = self._open(channel)
        if channel is not ToolChannel.FRAMES:
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

            recorder = self._journal.recorder(
                self._key, self._tool_name, channel, self._wake, self._protected
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
    """Журнал канала кадров: строка заголовка и размер тела вместо тела.

    Кадр, который не разбирается, отмечается строкой отказа: канал ведёт
    сам лончер, и молчать о нарушении протокола здесь нельзя.
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

    def opened(self, call_id: str, stream: LiveStream) -> None:
        """Наблюдатель RunRegistry: журнал открыт — насос запущен в текущем loop'е."""
        if not isinstance(stream, ToolStream):
            return

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

            logger.error("stream pump failed: %s", error, exc_info=error)


class ToolStreams:
    """Потоки инструментов: журнал вызовов и доступ к живым стримам хода.

    Потоковыми считаются инструменты, отмеченные при сборке реестра тулов, —
    только они запускают процессы, чей вывод есть смысл журналировать; без
    настроенного журнала потоков нет вовсе. Живые стримы живут в RunRegistry
    и закрываются вместе с ходом; файлы журнала переживают ход.

    Стрим открывает обвязка ToolRunLogger в потоке инструмента: call_id
    приезжает синтетическим полем схемы, очереди и обмена журналами между
    параллельными вызовами нет.
    """

    _STREAMABLE: ClassVar[set[str]] = set()

    @classmethod
    def configure(cls, journal: StreamStorePort) -> None:
        StreamJournalHub.configure(journal)

    @classmethod
    def active(cls) -> bool:
        """Журнал настроен: потоки пишутся и кнопки имеют смысл."""
        return StreamJournalHub.get() is not None

    @classmethod
    def journal(cls) -> StreamStorePort | None:
        return StreamJournalHub.get()

    @classmethod
    def live_scopes(cls) -> frozenset[str]:
        """Треды с живыми потоками: их нельзя удалять инструментом уборки."""
        return RunRegistry.live_scopes()

    @classmethod
    def mark_streamable(cls, names: Iterable[str]) -> None:
        cls._STREAMABLE.update(names)

    @classmethod
    def streamable(cls, tool_name: str) -> bool:
        if not cls.active():
            return False

        return tool_name in cls._STREAMABLE

    @classmethod
    def begin(
        cls, user_id: str, thread_id: str, call_id: str, tool_name: str
    ) -> ToolStream | None:
        """Открыть журнал вызова; сбой журнала не трогает ход инструмента."""
        journal = StreamJournalHub.get()
        if journal is None:
            return None

        context = RunRegistry.active(thread_id)
        if context is None:
            logger.warning(
                "stream journal skipped call %s of %s: no active run",
                call_id,
                tool_name,
            )
            return None

        try:
            key = StreamKey(user_id=user_id, thread_id=thread_id, call_id=call_id)
            stream = ToolStream(key, tool_name, journal, RunRegistry.live_prefixes())
        except (StreamJournalError, ValidationError):
            logger.warning(
                "stream journal refused call %s of %s",
                call_id,
                tool_name,
                exc_info=True,
            )
            return None

        context.add_stream(call_id, stream)
        return stream

    @classmethod
    def get(cls, thread_id: str, call_id: str) -> ToolStream | None:
        context = RunRegistry.active(thread_id)
        if context is None:
            return None

        stream = context.stream(call_id)
        if not isinstance(stream, ToolStream):
            return None

        return stream

    @classmethod
    def recorded_slice(
        cls,
        user_id: str,
        thread_id: str,
        call_id: str,
        offset: int,
        channel: JournalChannel,
    ) -> StreamSlice | None:
        """Окно журнала от смещения; отказ журнала — «нет данных», не сбой чата."""

        def read(journal: StreamStorePort, key: StreamKey) -> StreamSlice | None:
            return journal.slice_at(key, offset, channel)

        return cls._recorded(user_id, thread_id, call_id, read)

    @classmethod
    def recorded_slice_before(
        cls,
        user_id: str,
        thread_id: str,
        call_id: str,
        end: int,
        channel: JournalChannel,
    ) -> StreamSlice | None:
        """Окно перед смещением: прокрутка вверх; отказ — «нет данных»."""

        def read(journal: StreamStorePort, key: StreamKey) -> StreamSlice | None:
            return journal.slice_before(key, end, channel)

        return cls._recorded(user_id, thread_id, call_id, read)

    @classmethod
    def recorded_channels(
        cls, user_id: str, thread_id: str, call_id: str
    ) -> tuple[JournalChannel, ...]:
        """Каналы вызова с записью, доступные пользователю.

        Служебные каналы (конверт результата, вывод обвязки запуска) в
        панель не попадают: пишутся они всегда, читает их только разбор
        сбоев на сервере. Отказ журнала — пустой список вкладок.
        """
        journal = StreamJournalHub.get()
        if journal is None:
            return ()

        try:
            key = StreamKey(user_id=user_id, thread_id=thread_id, call_id=call_id)
            written = journal.channels_of(key)
        except (StreamJournalError, ValidationError):
            logger.warning("stream journal scan failed: %s", call_id, exc_info=True)
            return ()

        readable: list[JournalChannel] = []
        for channel in written:
            if not JournalChannels.visible(channel):
                continue

            readable.append(channel)

        return tuple(readable)

    @classmethod
    def _recorded(
        cls,
        user_id: str,
        thread_id: str,
        call_id: str,
        read: Callable[[StreamStorePort, StreamKey], StreamSlice | None],
    ) -> StreamSlice | None:
        journal = StreamJournalHub.get()
        if journal is None:
            return None

        try:
            key = StreamKey(user_id=user_id, thread_id=thread_id, call_id=call_id)
            return read(journal, key)
        except (StreamJournalError, ValidationError):
            logger.warning("stream journal read failed: %s", call_id, exc_info=True)
            return None

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        StreamJournalHub.reset()
        cls._STREAMABLE.clear()


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
        except StreamJournalError:
            logger.warning(
                "stream journal probe failed: %s", self._key.call_id, exc_info=True
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
