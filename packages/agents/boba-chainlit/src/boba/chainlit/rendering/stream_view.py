"""Живой вывод инструмента в канвасе: реестр потоков хода и насос показа.

Поток создаётся на каждый вызов потокового инструмента и живёт до конца хода.
Показ включает пользователь кнопкой на шаге инструмента; насос забирает
снапшоты окна по пробуждению от буфера, коалесцирует и пушит их в канал
канваса. Открытие другого файла в канвасе снимает насос.

Ошибки: наружу ничего не выходит; недоступный поток показывается в панели
объяснением.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from abc import abstractmethod
from collections import deque
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from boba.chainlit.rendering.canvas import CanvasContent, CanvasKind, CanvasPanel
from boba.toolkit.stream import StreamWindow, ToolStreamBuffer

__all__ = [
    "CanvasChannel",
    "SidebarChannel",
    "StreamAction",
    "StreamNote",
    "StreamScreen",
    "ToolStream",
    "ToolStreams",
    "show_stream_action",
]

logger = logging.getLogger(__name__)


class StreamAction(StrEnum):
    """Действие фронта для показа потока и поле его payload."""

    SHOW = "canvas_stream"
    CALL_ID = "call_id"


class StreamNote(StrEnum):
    """Формулировки статусной строки окна потока."""

    RUNNING = "выполняется…"
    FINISHED = "завершено"
    FAILED = "ошибка"
    STOPPED = "остановлено"
    GONE = "Поток этого вызова уже недоступен: ход завершён."

    @classmethod
    def status_of(cls, window: StreamWindow) -> str:
        status = str(cls.RUNNING)
        if window.closed:
            status = window.note

        if not window.dropped_bytes:
            return status

        return f"{status} · вытеснено {window.dropped_bytes} байт"


class StreamShowRequest(BaseModel):
    """Payload действия canvas_stream: разбирается на границе."""

    model_config = ConfigDict(extra="ignore")

    call_id: str


class CanvasChannel(Protocol):
    """Доставка содержимого потока в канвас; реализация выбирает транспорт."""

    @abstractmethod
    async def push(self, content: CanvasContent) -> None: ...


class SidebarChannel:
    """Канал через панель канваса: тот же путь, что у показа файлов."""

    async def push(self, content: CanvasContent) -> None:
        await CanvasPanel.show(content)


class ToolStream:
    """Живой вывод одного вызова инструмента: окно байтов плюс будильник.

    Создаётся в любом контексте — langchain зовёт колбэки sync-тулов из
    одноразового event loop'а, поэтому свой loop стрим не запоминает.
    Будильник подключает насос из loop'а приложения; вывод, пришедший до
    подключения, уже лежит в окне.
    """

    WINDOW_BYTES: ClassVar[int] = 64 * 1024

    def __init__(self, thread_id: str, call_id: str, tool_name: str) -> None:
        self._thread_id = thread_id
        self._call_id = call_id
        self._tool_name = tool_name
        self._lock = threading.Lock()
        self._waker: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None
        self._buffer = ToolStreamBuffer(self.WINDOW_BYTES, self._wake)

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def buffer(self) -> ToolStreamBuffer:
        return self._buffer

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
            logger.debug("stream wakeup after loop shutdown: %s", self._call_id)


class ToolStreams:
    """Реестр потоков: thread_id -> вызовы текущего хода.

    Потоковыми считаются инструменты, отмеченные при сборке реестра тулов, —
    только они запускают процессы, чей вывод есть смысл показывать.

    Стрим создаётся колбэком трейсера до запуска инструмента и ждёт в очереди
    claim по (тред, имя тула): обвязка в потоке инструмента забирает его и
    ставит тап. Параллельные одноимённые вызовы могут обменяться окнами —
    данные при этом не теряются.
    """

    _LOCK: ClassVar[threading.Lock] = threading.Lock()
    _STREAMS: ClassVar[dict[str, dict[str, ToolStream]]] = {}
    _PENDING: ClassVar[dict[tuple[str, str], deque[ToolStream]]] = {}
    _STREAMABLE: ClassVar[set[str]] = set()

    @classmethod
    def mark_streamable(cls, names: Iterable[str]) -> None:
        cls._STREAMABLE.update(names)

    @classmethod
    def streamable(cls, tool_name: str) -> bool:
        return tool_name in cls._STREAMABLE

    @classmethod
    def begin(cls, thread_id: str, call_id: str, tool_name: str) -> ToolStream:
        stream = ToolStream(thread_id, call_id, tool_name)

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

        stream.buffer.close(note)

    @classmethod
    def get(cls, thread_id: str, call_id: str) -> ToolStream | None:
        with cls._LOCK:
            return cls._STREAMS.get(thread_id, {}).get(call_id)

    @classmethod
    def drop_thread(cls, thread_id: str) -> None:
        """Конец хода: насос снимается, окна вызовов освобождаются."""
        StreamScreen.leave(thread_id)

        with cls._LOCK:
            cls._STREAMS.pop(thread_id, None)
            for key in list(cls._PENDING):
                if key[0] == thread_id:
                    del cls._PENDING[key]

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
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
    async def gone(cls, call_id: str, channel: CanvasChannel) -> None:
        """Потока нет: панель объясняет это вместо содержимого."""
        content = CanvasContent(
            kind=CanvasKind.NOTICE,
            path=f"stream://{call_id}",
            label="поток инструмента",
            note=str(StreamNote.GONE),
        )
        await channel.push(content)

    @classmethod
    def _forget(cls, thread_id: str, task: asyncio.Task[None]) -> None:
        if cls._PUMPS.get(thread_id) is task:
            del cls._PUMPS[thread_id]

    @classmethod
    async def _pump(cls, stream: ToolStream, channel: CanvasChannel) -> None:
        """Снапшот на каждое пробуждение; закрытое окно пушится последний раз."""
        try:
            event = stream.attach_waker()

            while True:
                event.clear()
                window = stream.buffer.snapshot()
                await channel.push(cls._content(stream, window))

                if window.closed:
                    return

                await event.wait()
                await asyncio.sleep(cls.COALESCE_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("stream pump stopped: push failed", exc_info=True)

    @classmethod
    def _content(cls, stream: ToolStream, window: StreamWindow) -> CanvasContent:
        return CanvasContent(
            kind=CanvasKind.STREAM,
            path=f"stream://{stream.call_id}",
            label=stream.tool_name,
            text=window.text,
            note=StreamNote.status_of(window),
            nonce=str(next(cls._REVISIONS)),
        )


async def show_stream_action(thread_id: str, payload: Mapping[str, object]) -> None:
    """Клик по кнопке потока: включить насос либо объяснить его отсутствие."""
    request = StreamShowRequest.model_validate(payload)
    channel = SidebarChannel()

    stream = ToolStreams.get(thread_id, request.call_id)
    if stream is None:
        await StreamScreen.gone(request.call_id, channel)
        return

    await StreamScreen.show(thread_id, stream, channel)
