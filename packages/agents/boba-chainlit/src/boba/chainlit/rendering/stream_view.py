"""Живой вывод инструмента в канвасе: журнал вызовов, реестр хода и насос.

Каждый вызов потокового инструмента пишется в журнал (файл в служебном томе
пользователя) и живёт после конца хода. Показ включает пользователь кнопкой
на шаге: живой вызов тянет насос — хвост журнала по пробуждениям; завершённый
читается из файла. Перемотка ходит по журналу окнами фиксированного размера,
целиком файл во фронт не поднимается.

Ошибки: наружу ничего не выходит; недоступный поток показывается в панели
объяснением, сбой журнала гасится с записью в лог.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from abc import abstractmethod
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from boba.chainlit.chat.data.object_key import StreamUrl
from boba.chainlit.chat.data.stream_journal import (
    StreamJournal,
    StreamJournalError,
    StreamJournalHub,
    StreamKey,
    StreamRecorder,
    StreamSlice,
)
from boba.chainlit.rendering.canvas import (
    CanvasContent,
    CanvasKind,
    CanvasPanel,
    StreamPos,
)

__all__ = [
    "CanvasChannel",
    "SidebarChannel",
    "StreamAction",
    "StreamNote",
    "StreamScreen",
    "ToolStream",
    "ToolStreams",
    "show_stream_action",
    "window_stream_action",
]

logger = logging.getLogger(__name__)


class StreamAction(StrEnum):
    """Действия фронта для потока и поля их payload."""

    SHOW = "canvas_stream"
    WINDOW = "canvas_stream_window"
    CALL_ID = "call_id"
    OFFSET = "offset"


class StreamNote(StrEnum):
    """Формулировки статусной строки окна потока."""

    RUNNING = "выполняется…"
    FINISHED = "завершено"
    FAILED = "ошибка"
    STOPPED = "остановлено"
    GONE = "Журнал этого вызова недоступен: журналирование не велось."

    @classmethod
    def status_of(cls, piece: StreamSlice) -> str:
        if not piece.closed:
            return str(cls.RUNNING)

        return piece.note


class StreamShowRequest(BaseModel):
    """Payload действия canvas_stream: вызов и режим показа.

    follow — к концу файла: живой вызов следит за хвостом насосом,
    завершённый показывает последнее окно. Без follow — окно с начала.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str
    follow: bool = False


class StreamWindowRequest(BaseModel):
    """Payload действия canvas_stream_window: вызов и одна из границ окна.

    offset — окно вперёд от смещения (прокрутка вниз); before — окно,
    заканчивающееся на смещении (прокрутка вверх). Ровно одно из двух.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str
    offset: int | None = None
    before: int | None = None

    @model_validator(mode="after")
    def _one_bound(self) -> Self:
        if (self.offset is None) == (self.before is None):
            msg = "canvas_stream_window: ровно одно из offset и before"
            raise ValueError(msg)

        return self


class CanvasChannel(Protocol):
    """Доставка содержимого потока в канвас; реализация выбирает транспорт."""

    @abstractmethod
    async def push(self, content: CanvasContent) -> None: ...


class SidebarChannel:
    """Канал через панель канваса: тот же путь, что у показа файлов."""

    async def push(self, content: CanvasContent) -> None:
        await CanvasPanel.show(content)


class ToolStream:
    """Живой вызов инструмента: рекордер журнала плюс будильник насоса.

    Создаётся в любом контексте — langchain зовёт колбэки sync-тулов из
    одноразового event loop'а, поэтому свой loop стрим не запоминает.
    Будильник подключает насос из loop'а приложения; вывод, пришедший до
    подключения, уже лежит в журнале.
    """

    def __init__(
        self,
        key: StreamKey,
        tool_name: str,
        journal: StreamJournal,
        protected_logs: frozenset[str],
    ) -> None:
        self._key = key
        self._tool_name = tool_name
        self._lock = threading.Lock()
        self._waker: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None
        self._recorder = journal.recorder(
            key, tool_name, self._wake, protected_logs
        )

    @property
    def key(self) -> StreamKey:
        return self._key

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def recorder(self) -> StreamRecorder:
        return self._recorder

    def tail(self, window: int) -> StreamSlice:
        return self._recorder.tail(window)

    def attach_waker(self) -> asyncio.Event:
        """Событие пробуждения в текущем loop'е; прежний слушатель забывается."""
        event = asyncio.Event()
        loop = asyncio.get_running_loop()

        with self._lock:
            self._waker = (loop, event)

        return event

    def _wake(self) -> None:
        with self._lock:
            waker = self._waker

        if waker is None:
            return

        loop, event = waker
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            logger.debug("stream wakeup after loop shutdown: %s", self._key.call_id)


class ToolStreams:
    """Реестр потоков: журнал вызовов плюс живые стримы текущих ходов.

    Потоковыми считаются инструменты, отмеченные при сборке реестра тулов, —
    только они запускают процессы, чей вывод есть смысл журналировать; без
    настроенного журнала потоков нет вовсе.

    Стрим создаётся колбэком трейсера до запуска инструмента и ждёт в очереди
    claim по (тред, имя тула): обвязка в потоке инструмента забирает его и
    ставит тап. Параллельные одноимённые вызовы могут обменяться журналами —
    данные при этом не теряются.
    """

    _LOCK: ClassVar[threading.Lock] = threading.Lock()
    _STREAMS: ClassVar[dict[str, dict[str, ToolStream]]] = {}
    _PENDING: ClassVar[dict[tuple[str, str], deque[ToolStream]]] = {}
    _STREAMABLE: ClassVar[set[str]] = set()

    @classmethod
    def configure(cls, journal: StreamJournal) -> None:
        StreamJournalHub.configure(journal)

    @classmethod
    def active(cls) -> bool:
        """Журнал настроен: потоки пишутся и кнопки имеют смысл."""
        return StreamJournalHub.get() is not None

    @classmethod
    def journal(cls) -> StreamJournal | None:
        return StreamJournalHub.get()

    @classmethod
    def live_threads(cls) -> frozenset[str]:
        """Треды с живыми потоками: их нельзя удалять инструментом уборки."""
        with cls._LOCK:
            return frozenset(cls._STREAMS)

    @classmethod
    def live_logs(cls) -> frozenset[str]:
        """Rel-пути логов живых вызовов: вытеснять их при нехватке места нельзя."""
        with cls._LOCK:
            return frozenset(cls._live_log_paths())

    @classmethod
    def _live_log_paths(cls) -> Iterator[str]:
        for thread_id, calls in cls._STREAMS.items():
            for call_id in calls:
                yield f"{thread_id}/{call_id}.log"

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

        try:
            key = StreamKey(
                user_id=user_id, thread_id=thread_id, call_id=call_id
            )
            stream = ToolStream(key, tool_name, journal, cls.live_logs())
        except (StreamJournalError, ValidationError):
            logger.warning(
                "stream journal refused call %s of %s", call_id, tool_name,
                exc_info=True,
            )
            return None

        with cls._LOCK:
            cls._STREAMS.setdefault(thread_id, {})[call_id] = stream
            cls._PENDING.setdefault((thread_id, tool_name), deque()).append(stream)

        return stream

    @classmethod
    def claim(cls, thread_id: str, tool_name: str) -> ToolStream | None:
        """Забрать стрим своего вызова; None — вызов не потоковый."""
        with cls._LOCK:
            queue = cls._PENDING.get((thread_id, tool_name))
            if not queue:
                return None
            return queue.popleft()

    @classmethod
    def finish(cls, thread_id: str, call_id: str, note: str) -> None:
        stream = cls.get(thread_id, call_id)
        if stream is None:
            return

        # незаклеймленный слот упавшего вызова не должен достаться следующему
        with cls._LOCK:
            queue = cls._PENDING.get((thread_id, stream.tool_name))
            if queue is not None and stream in queue:
                queue.remove(stream)

        stream.recorder.close(note)

    @classmethod
    def get(cls, thread_id: str, call_id: str) -> ToolStream | None:
        with cls._LOCK:
            return cls._STREAMS.get(thread_id, {}).get(call_id)

    @classmethod
    def drop_thread(cls, thread_id: str) -> None:
        """Конец хода: насос снимается, живые стримы забываются, журнал остаётся."""
        StreamScreen.leave(thread_id)

        with cls._LOCK:
            streams = cls._STREAMS.pop(thread_id, {})
            for key in list(cls._PENDING):
                if key[0] == thread_id:
                    del cls._PENDING[key]

        for stream in streams.values():
            if not stream.recorder.closed:
                stream.recorder.close(str(StreamNote.STOPPED))

    @classmethod
    def recorded_slice(
        cls, user_id: str, thread_id: str, call_id: str, offset: int
    ) -> StreamSlice | None:
        """Окно журнала; любой отказ журнала — «нет данных», не сбой чата."""
        journal = StreamJournalHub.get()
        if journal is None:
            return None

        try:
            key = StreamKey(
                user_id=user_id, thread_id=thread_id, call_id=call_id
            )
            return journal.slice_at(key, offset)
        except (StreamJournalError, ValidationError):
            logger.warning(
                "stream journal read failed: %s", call_id, exc_info=True
            )
            return None

    @classmethod
    def recorded_slice_before(
        cls, user_id: str, thread_id: str, call_id: str, end: int
    ) -> StreamSlice | None:
        """Окно перед смещением: прокрутка вверх; отказ — «нет данных»."""
        journal = StreamJournalHub.get()
        if journal is None:
            return None

        try:
            key = StreamKey(
                user_id=user_id, thread_id=thread_id, call_id=call_id
            )
            return journal.slice_before(key, end)
        except (StreamJournalError, ValidationError):
            logger.warning(
                "stream journal read failed: %s", call_id, exc_info=True
            )
            return None

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        StreamJournalHub.reset()
        with cls._LOCK:
            cls._STREAMS.clear()
            cls._PENDING.clear()
            cls._STREAMABLE.clear()


class StreamScreen:
    """Насос показа: один поток на тред, переключение снимает предыдущий."""

    COALESCE_SEC: ClassVar[float] = 0.3
    """Пауза после пробуждения: болтливый инструмент не заливает сокет."""

    _PUMPS: ClassVar[dict[str, asyncio.Task[None]]] = {}
    _REVISIONS: ClassVar[itertools.count[int]] = itertools.count(1)
    """Сквозной номер показа: едет в nonce, по нему фронт видит смену props."""

    @classmethod
    async def show(
        cls, thread_id: str, stream: ToolStream, channel: CanvasChannel
    ) -> asyncio.Task[None]:
        cls.leave(thread_id)
        task = asyncio.create_task(cls._pump(stream, channel))
        cls._PUMPS[thread_id] = task
        task.add_done_callback(lambda done: cls._forget(thread_id, done))
        return task

    @classmethod
    def leave(cls, thread_id: str) -> None:
        """Пользователь ушёл с потока: насос больше не трогает панель."""
        if task := cls._PUMPS.pop(thread_id, None):
            task.cancel()

    @classmethod
    async def recorded(
        cls,
        thread_id: str,
        call_id: str,
        piece: StreamSlice,
        channel: CanvasChannel,
    ) -> None:
        """Показ окна записанного журнала: без насоса, один кадр."""
        await channel.push(cls.content(thread_id, call_id, "", piece))

    @classmethod
    async def gone(cls, call_id: str, channel: CanvasChannel) -> None:
        """Журнала нет: панель объясняет это вместо содержимого."""
        content = CanvasContent(
            kind=CanvasKind.NOTICE,
            path=f"stream://{call_id}",
            label="поток инструмента",
            note=str(StreamNote.GONE),
        )
        await channel.push(content)

    @classmethod
    def content(
        cls, thread_id: str, call_id: str, label: str, piece: StreamSlice
    ) -> CanvasContent:
        return CanvasContent(
            kind=CanvasKind.STREAM,
            path=f"stream://{call_id}",
            label=label,
            url=StreamUrl.path(thread_id, call_id),
            text=piece.text,
            note=StreamNote.status_of(piece),
            nonce=str(next(cls._REVISIONS)),
            stream=StreamPos(
                offset=piece.offset,
                end=piece.end,
                size=piece.size,
                window=piece.window,
                closed=piece.closed,
            ),
        )

    @classmethod
    def _forget(cls, thread_id: str, task: asyncio.Task[None]) -> None:
        if cls._PUMPS.get(thread_id) is task:
            del cls._PUMPS[thread_id]

    @classmethod
    async def _pump(cls, stream: ToolStream, channel: CanvasChannel) -> None:
        """Хвост журнала на каждое пробуждение; закрытый пушится последний раз."""
        try:
            event = stream.attach_waker()

            while True:
                event.clear()
                piece = stream.tail(StreamJournal.WINDOW_BYTES)
                await channel.push(
                    cls.content(
                        stream.key.thread_id,
                        stream.key.call_id,
                        stream.tool_name,
                        piece,
                    )
                )

                if piece.closed:
                    return

                await event.wait()
                await asyncio.sleep(cls.COALESCE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("stream pump stopped: push failed", exc_info=True)


async def show_stream_action(
    user_id: str, thread_id: str, payload: Mapping[str, object]
) -> None:
    """Показ потока: по умолчанию окно с начала файла; follow — к концу,
    у живого вызова с насосом-слежением за хвостом."""
    request = StreamShowRequest.model_validate(payload)
    channel = SidebarChannel()

    if request.follow:
        if stream := ToolStreams.get(thread_id, request.call_id):
            await StreamScreen.show(thread_id, stream, channel)
            return

    StreamScreen.leave(thread_id)

    offset = 0
    if request.follow:
        offset = -1

    piece = ToolStreams.recorded_slice(
        user_id, thread_id, request.call_id, offset=offset
    )
    if piece is None:
        await StreamScreen.gone(request.call_id, channel)
        return

    await StreamScreen.recorded(thread_id, request.call_id, piece, channel)


async def window_stream_action(
    user_id: str, thread_id: str, payload: Mapping[str, object]
) -> dict[str, Any]:
    """Прокрутка: окно журнала по границе; уход из хвоста снимает насос."""
    request = StreamWindowRequest.model_validate(payload)

    StreamScreen.leave(thread_id)

    if request.before is not None:
        piece = ToolStreams.recorded_slice_before(
            user_id, thread_id, request.call_id, end=request.before
        )
    else:
        offset = request.offset
        if offset is None:
            offset = 0
        piece = ToolStreams.recorded_slice(
            user_id, thread_id, request.call_id, offset=offset
        )

    if piece is None:
        return {}

    content = StreamScreen.content(thread_id, request.call_id, "", piece)
    return content.props()
