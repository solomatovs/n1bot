"""Канвас: панель справа от чата, вьюверы файлов, окна чтения и слежение.

Панель показывает содержимое одного файла — того, что открыла LLM тулом или
ткнул пользователь. Содержимое едет во фронт один раз при открытии; дальше
сервер шлёт только сигналы об изменении (window_message), а фронт сам
запрашивает окно или перечитывает файл — панель не переоткрывается и не
перерисовывается целиком. Текст любого размера ходит окнами по смещению в
файле, как less: целиком в память не поднимается ни на сервере, ни во фронте.

Слежение за показанным файлом живёт, пока панель открыта: снимается уходом
на другой файл, закрытием панели (canvas_leave) или смертью всех сокетов
треда. Живой журнал будит слежение записью, файлы workspace поллятся.

Ошибки: CanvasError — файл некому показать или он не отрисовался.
Действия фронта и слежение своих ошибок наружу не выпускают: недоступный
поток показывается объяснением в панели, сбои слежения гасятся в лог.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import threading
import uuid
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

import chainlit as cl
from boba.chainlit.agent.toolrun.run_log import CallStream
from boba.chainlit.canvas.journal import (
    JournalWindow,
    StreamJournalError,
    StreamJournalHub,
    StreamKey,
    StreamRecorderPort,
    StreamSlice,
    StreamStorePort,
    WindowAlign,
)
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import (
    StorageClient,
    StorageError,
    StorageNotFoundError,
)
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.keys import CanvasFileUrl, ObjectKey, StreamUrl
from boba.chainlit.domain.run import LiveStream, RunRegistry
from boba.toolkit.channels import JournalChannel, JournalChannels, ToolChannel
from boba.toolkit.result import CustomElementResult, DiagramResult
from boba.toolkit.stream import ChannelSinks, StreamSink
from boba.workspace.launcher import ReadWindow
from chainlit.data import get_data_layer

__all__ = [
    "AudioViewer",
    "CanvasAction",
    "CanvasContent",
    "CanvasError",
    "CanvasErrorKind",
    "CanvasKind",
    "CanvasPanel",
    "CanvasPush",
    "CanvasRegistry",
    "CanvasSignal",
    "CanvasViewer",
    "CanvasWatch",
    "FileViewer",
    "ImageViewer",
    "JournalWatchSource",
    "LogViewer",
    "MarkdownViewer",
    "OpenedCanvas",
    "PanelStorage",
    "PdfViewer",
    "RenderStatus",
    "RenderVerdict",
    "RenderVerdicts",
    "SignalTransport",
    "StorageHashSource",
    "StorageStatSource",
    "StorageWindows",
    "StreamActions",
    "StreamNote",
    "StreamPath",
    "StreamPos",
    "ToolStream",
    "ToolStreams",
    "VideoViewer",
    "WatchProbe",
    "WatchSource",
]

logger = logging.getLogger(__name__)


class CanvasErrorKind(StrEnum):
    """Коды отказов панели: уезжают в ErrorResult.error_kind."""

    BAD_PATH = "bad_path"
    NO_VIEWER = "no_canvas_viewer"
    FILE_NOT_FOUND = "file_not_found"
    BAD_FILE = "bad_file"
    TOO_LARGE = "file_too_large"
    RENDER_FAILED = "render_failed"


class CanvasError(RefusalError):
    """Панель не показала файл; kind — код причины, текст готов для LLM.

    kind шире собственного enum панели: вьювер поднимает сюда свои коды
    (file_not_found и подобные) как есть.
    """


class CanvasAction(StrEnum):
    """Действия фронта для канваса и поля их payload."""

    OPEN = "canvas_open"
    CONTENT = "canvas_content"
    STATUS = "canvas_render_status"
    SHOW = "canvas_stream"
    WINDOW = "canvas_stream_window"
    LEAVE = "canvas_leave"
    PATH = "path"
    NONCE = "nonce"
    CALL_ID = "call_id"
    OFFSET = "offset"


class RenderStatus(StrEnum):
    """Исход рендера в браузере."""

    RENDERED = "rendered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    """Браузер не ответил за отведённое время (вкладка закрыта)."""


class RenderVerdict(BaseModel):
    """Вердикт рендера одного показа: статус и текст ошибки mermaid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RenderStatus
    message: str = ""


class RenderReport(BaseModel):
    """Payload действия canvas_render_status: разбирается на границе."""

    model_config = ConfigDict(extra="ignore")

    nonce: str
    ok: bool
    error: str = ""

    def verdict(self) -> RenderVerdict:
        if self.ok:
            return RenderVerdict(status=RenderStatus.RENDERED)

        return RenderVerdict(status=RenderStatus.FAILED, message=self.error)


class RenderVerdicts:
    """Ожидания вердиктов рендера: nonce показа -> future с исходом.

    Валидатор синтаксиса один — mermaid.js в браузере: сервер не гадает,
    а ждёт его ответ. Отчёт на незнакомый nonce (после таймаута или от
    повторного рендера) молча пропускается — ждать его уже некому.
    """

    _WAITERS: ClassVar[dict[str, asyncio.Future[RenderVerdict]]] = {}

    @classmethod
    def expect(cls, nonce: str) -> None:
        """Начать ждать вердикт; зовётся до показа элемента, чтобы не было гонки."""
        loop = asyncio.get_running_loop()
        cls._WAITERS[nonce] = loop.create_future()

    @classmethod
    def report(cls, payload: Mapping[str, object]) -> None:
        """Принять отчёт браузера; битый payload — ValidationError наверх.

        Ожидание остаётся в реестре: браузер может ответить раньше, чем
        вьювер дойдёт до wait, — забирает и чистит запись только wait.
        """
        parsed = RenderReport.model_validate(payload)

        waiter = cls._WAITERS.get(parsed.nonce)
        if waiter is None:
            logger.debug(
                "render verdict dropped: nobody waits for nonce %s (ok=%s)",
                parsed.nonce,
                parsed.ok,
            )
            return

        if waiter.done():
            return

        waiter.set_result(parsed.verdict())

    @classmethod
    async def wait(cls, nonce: str, timeout_sec: float) -> RenderVerdict:
        """Дождаться вердикта; молчание браузера — UNKNOWN, не ошибка."""
        waiter = cls._WAITERS.get(nonce)
        if waiter is None:
            return RenderVerdict(status=RenderStatus.UNKNOWN)

        try:
            return await asyncio.wait_for(waiter, timeout_sec)
        except TimeoutError:
            return RenderVerdict(status=RenderStatus.UNKNOWN)
        finally:
            cls._WAITERS.pop(nonce, None)


class CanvasKind(StrEnum):
    """Чем рисовать содержимое в панели: выбирает вьювер, рисует CanvasView."""

    MERMAID = "mermaid"
    IMAGE = "image"
    PDF = "pdf"
    TEXT = "text"
    VIDEO = "video"
    AUDIO = "audio"
    STREAM = "stream"
    """Текст окнами по смещению в файле: журнал вызова или файл workspace."""
    NOTICE = "notice"
    """Формат показать некому: панель объясняет это вместо содержимого."""


class StreamPos(BaseModel):
    """Координаты окна потока в файле: где стоим и сколько всего."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    offset: int
    end: int
    """Байт за последним в окне: сюда фронт стыкует следующее окно."""
    size: int
    window: int
    closed: bool


class CanvasContent(BaseModel):
    """Описание содержимого панели: что рисовать и откуда взять.

    Сервер описывает, фронт рисует — содержимое едет во фронт при открытии,
    все дальнейшие обновления фронт запрашивает сам по сигналам слежения.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CanvasKind
    path: str
    label: str
    url: str = ""
    """Ссылка на файл в storage: картинки, pdf, видео, аудио, скачивание."""
    text: str = ""
    """Содержимое как текст: спека mermaid, markdown, окно лога."""
    mime: str = ""
    note: str = ""
    """Причина для notice: почему файл не показан."""
    nonce: str = ""
    """Метка показа: по ней ждут вердикт рендера и снимают слежение."""
    stream: StreamPos | None = None
    """Окно потока: есть только у kind = stream."""
    channel: str = ""
    """Показанный канал журнала вызова; пусто — содержимое не из журнала."""
    channels: tuple[str, ...] = ()
    """Каналы вызова с записью: фронт рисует их вкладками над окном."""

    def props(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class OpenedCanvas:
    """Открытый в панели файл: подпись, путь, метка показа и элемент ленты.

    link — результат, который остаётся в переписке и по клику открывает панель:
    кнопка-ссылка для обычных файлов, отрисованная диаграмма для .mmd.
    """

    label: str
    path: str
    nonce: str
    link: CustomElementResult | DiagramResult


class CanvasPush(Protocol):
    """Показ содержимого в панели; повторный вызов заменяет его."""

    @abstractmethod
    async def __call__(self, content: CanvasContent) -> None: ...


class WatchProbe(BaseModel):
    """Состояние наблюдаемого файла: по смене revision фронту уходит сигнал.

    closed — статус записи для показа (running или итог вызова); final —
    источник исчерпан и меняться больше не будет: слежение снимается.
    Файл workspace closed для показа, но не final — его могут переписать.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: str
    size: int
    closed: bool = False
    final: bool = False
    note: str = ""


class WatchSource(Protocol):
    """Источник состояния наблюдаемого файла для слежения панели."""

    @abstractmethod
    async def probe(self) -> WatchProbe | None:
        """Текущее состояние; None — файла больше нет, следить не за чем."""
        ...

    @abstractmethod
    def attach_waker(self) -> asyncio.Event | None:
        """Будильник записи в текущем loop'е; None — источник только поллится."""
        ...


class CanvasViewer(Protocol):
    """Файлы одного вида: какие расширения умеет и чем их описать."""

    suffixes: ClassVar[frozenset[str]]
    """Расширения вьювера: панель перечисляет их, когда файл показать некому."""

    @abstractmethod
    def handles(self, name: str) -> bool:
        """Берётся ли вьювер за файл с таким именем."""
        ...

    @abstractmethod
    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        """Показать файл через push; вернуть подпись, путь и элемент для ленты."""
        ...

    @abstractmethod
    async def content(self, key: ObjectKey) -> CanvasContent:
        """Описание файла для панели; панель его только доставляет."""
        ...

    @abstractmethod
    def watch_source(self, key: ObjectKey) -> WatchSource | None:
        """Источник слежения за файлом; None — за файлом не следят."""
        ...


class CanvasRegistry:
    """Вьюверы канваса: файл -> кто его рисует; порядок регистрации решает спор.

    Реестр общий на приложение: панель открывается и вне сборки тулов — из
    действия пользователя. Тулы собираются на каждую сессию, поэтому
    регистрация идемпотентна по типу вьювера.
    """

    _VIEWERS: ClassVar[list[CanvasViewer]] = []

    @classmethod
    def register(cls, viewer: CanvasViewer) -> None:
        kind = type(viewer)

        for index, known in enumerate(cls._VIEWERS):
            if type(known) is not kind:
                continue
            cls._VIEWERS[index] = viewer
            return

        cls._VIEWERS.append(viewer)

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        cls._VIEWERS.clear()

    @classmethod
    def viewer_for(cls, name: str) -> CanvasViewer | None:
        for viewer in cls._VIEWERS:
            if viewer.handles(name):
                return viewer
        return None

    @classmethod
    def viewers_hint(cls) -> str:
        """Какие вьюверы зарегистрированы — для логов и диагностики."""
        if not cls._VIEWERS:
            return "none registered"

        names: list[str] = []
        for viewer in cls._VIEWERS:
            names.append(type(viewer).__name__)

        return ", ".join(sorted(names))

    @classmethod
    def suffixes_hint(cls) -> str:
        """Какие расширения панель показывает — для пользователя и LLM."""
        known: set[str] = set()
        for viewer in cls._VIEWERS:
            known.update(viewer.suffixes)

        if not known:
            return "no viewers registered"

        return ", ".join(sorted(known))


class SignalTransport(Protocol):
    """Доставка сигналов слежения во все живые сокеты треда."""

    @abstractmethod
    def alive(self, thread_id: str) -> bool:
        """Есть ли у треда живые сокеты; без них слежение не нужно."""
        ...

    @abstractmethod
    async def send(self, thread_id: str, payload: Mapping[str, object]) -> None: ...


class CanvasSignal(BaseModel):
    """Сигнал фронту: показанный файл изменился, содержимое не едет.

    Фронт сам решает, что запросить: окно после текущего (лог), описание
    целиком (диаграмма) или перезагрузку по ссылке (картинка, pdf).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    TYPE: ClassVar[str] = "boba:canvas"
    """Метка канала window_message: по ней фронт отличает сигнал от чужого."""

    path: str
    nonce: str
    revision: str
    size: int
    closed: bool
    note: str

    def payload(self) -> dict[str, Any]:
        body = self.model_dump(mode="json")
        body["type"] = self.TYPE
        return body


class CanvasWatch:
    """Слежение за показанным файлом: один вотчер на тред, жизнь — панель.

    Пуш-источник (живой журнал) будит вотчер записью, файловый поллится.
    Смена файла в панели снимает предыдущее слежение; закрытие панели или
    смерть всех сокетов треда останавливают вотчер.
    """

    POLL_SEC: ClassVar[float] = 1.0
    """Период опроса файловых источников и проверки живости треда."""

    COALESCE_SEC: ClassVar[float] = 0.3
    """Пауза после пробуждения: болтливый инструмент не заливает сокет."""

    _TRANSPORT: ClassVar[SignalTransport | None] = None
    _WATCHES: ClassVar[dict[str, CanvasWatch]] = {}

    def __init__(
        self,
        thread_id: str,
        path: str,
        nonce: str,
        source: WatchSource,
        seen: str,
    ) -> None:
        self._thread_id = thread_id
        self._path = path
        self._nonce = nonce
        self._source = source
        self._seen = seen
        self._task: asyncio.Task[None] | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def nonce(self) -> str:
        return self._nonce

    @classmethod
    def configure(cls, transport: SignalTransport) -> None:
        cls._TRANSPORT = transport

    @classmethod
    def enabled(cls) -> bool:
        """Транспорт настроен: слежение возможно и источники имеют смысл."""
        return cls._TRANSPORT is not None

    @classmethod
    def reset(cls) -> None:
        """Сброс: пользуются тесты, приложению это не нужно."""
        for thread_id in list(cls._WATCHES):
            cls.drop(thread_id)
        cls._TRANSPORT = None

    @classmethod
    def show(
        cls,
        thread_id: str,
        path: str,
        nonce: str,
        source: WatchSource,
        seen: str = "",
    ) -> None:
        """Начать следить за файлом панели; прежнее слежение треда снимается.

        seen — revision показанного содержимого: сигнал уходит только на
        изменения после него; пустой — точка отсчёта берётся первым опросом.
        """
        if cls._TRANSPORT is None:
            return

        cls.drop(thread_id)

        watch = cls(thread_id, path, nonce, source, seen)
        cls._WATCHES[thread_id] = watch
        watch._task = asyncio.create_task(watch._run())

    @classmethod
    def leave(cls, thread_id: str, nonce: str) -> None:
        """Панель ушла с файла; чужой nonce слежение не трогает.

        Гонка переоткрытия: leave старого показа приходит после show нового —
        сравнение nonce не даёт ему снять свежее слежение.
        """
        watch = cls._WATCHES.get(thread_id)
        if watch is None:
            return

        if nonce and watch._nonce != nonce:
            return

        cls.drop(thread_id)

    @classmethod
    def drop(cls, thread_id: str) -> None:
        watch = cls._WATCHES.pop(thread_id, None)
        if watch is None:
            return

        if watch._task is not None:
            watch._task.cancel()

    @classmethod
    def watching(cls, thread_id: str) -> str | None:
        """Путь под слежением; None — тред ни на что не смотрит."""
        watch = cls._WATCHES.get(thread_id)
        if watch is None:
            return None

        return watch._path

    async def _run(self) -> None:
        transport = self._TRANSPORT
        if transport is None:
            return

        try:
            waker = self._source.attach_waker()

            while self._current():
                if not transport.alive(self._thread_id):
                    return

                probe = await self._source.probe()
                if probe is None:
                    return

                stop = await self._advance(transport, probe)
                if stop:
                    return

                await self._pause(waker)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("canvas watch stopped: %s", self._path, exc_info=True)
        finally:
            self._forget()

    async def _advance(self, transport: SignalTransport, probe: WatchProbe) -> bool:
        """Сигнал на смену revision; исчерпанный источник досылается и снимается."""
        if not self._seen:
            self._seen = probe.revision
            return probe.final

        if probe.revision != self._seen:
            self._seen = probe.revision
            await transport.send(self._thread_id, self._signal(probe).payload())

        return probe.final

    def _signal(self, probe: WatchProbe) -> CanvasSignal:
        return CanvasSignal(
            path=self._path,
            nonce=self._nonce,
            revision=probe.revision,
            size=probe.size,
            closed=probe.closed,
            note=probe.note,
        )

    def _current(self) -> bool:
        return self._WATCHES.get(self._thread_id) is self

    def _forget(self) -> None:
        if self._WATCHES.get(self._thread_id) is self:
            del self._WATCHES[self._thread_id]

    async def _pause(self, waker: asyncio.Event | None) -> None:
        if waker is None:
            await asyncio.sleep(self.POLL_SEC)
            return

        try:
            await asyncio.wait_for(waker.wait(), timeout=self.POLL_SEC)
        except TimeoutError:
            return

        waker.clear()
        await asyncio.sleep(self.COALESCE_SEC)


class StreamPath(BaseModel):
    """Псевдо-путь панели для канала журнала: сборка и разбор в одном месте.

    Канал входит в путь: панель адресует показом один файл, а каналы вызова —
    разные файлы, и слежение с окнами обязано их различать. Канал проверяется
    на доступность здесь: путь с закрытым каналом собрать нельзя.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SCHEME: ClassVar[str] = "stream://"

    call_id: str
    channel: JournalChannel

    @field_validator("channel")
    @classmethod
    def _readable(cls, value: JournalChannel) -> JournalChannel:
        if not JournalChannels.visible(value):
            msg = f"channel is not readable: {value.value}"
            raise ValueError(msg)

        return value

    def render(self) -> str:
        return f"{self.SCHEME}{self.call_id}/{self.channel.value}"

    @classmethod
    def is_stream(cls, path: str) -> bool:
        """Путь адресует журнал вызова, а не файл workspace."""
        return path.startswith(cls.SCHEME)

    @classmethod
    def parse(cls, path: str) -> StreamPath | None:
        """Разбор пути показа; None — путь не потоковый или канал закрыт."""
        if not cls.is_stream(path):
            return None

        call_id, _, name = path[len(cls.SCHEME) :].partition("/")
        if not call_id:
            return None

        channel = JournalChannels.parse_visible(name)
        if channel is None:
            return None

        return cls(call_id=call_id, channel=channel)


class StreamNote(StrEnum):
    """Экранные тексты статусной строки окна потока.

    Исходы закрытого вызова приходят из журнала словами CallOutcome —
    их пишет обвязка вызова, панель показывает как есть.
    """

    RUNNING = "running…"
    GONE = "The log of this call is unavailable: journaling was not active."

    @classmethod
    def status_of(cls, piece: StreamSlice) -> str:
        if not piece.closed:
            return str(cls.RUNNING)

        return piece.note


class ToolStream(ChannelSinks, CallStream, LiveStream):
    """Живой вызов инструмента: рекордеры каналов плюс будильник слежения.

    Создаётся в потоке исполнения инструмента; свой event loop стрим не
    запоминает. Будильник подключает слежение из loop'а приложения; вывод,
    пришедший до подключения, уже лежит в журнале. Канал stdout открывается
    сразу — панель находит файл до первого байта; остальные — по обращению.
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
        self._waker: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None
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
        """Приёмник канала; рекордер открывается при первом обращении."""
        return self._open(channel)

    def close(self, note: str) -> None:
        """Закрыть все каналы вызова одной пометкой; повтор безвреден."""
        with self._lock:
            recorders = list(self._recorders.values())

        for recorder in recorders:
            recorder.close(note)

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


class StorageStatSource(WatchSource):
    """Слежение за файлом workspace: версия по размеру и времени записи.

    Одного размера мало — правка той же длины (переписанная строка, тот же
    график) его не меняет, и обновление не дошло бы до панели.
    """

    def __init__(self, storage: StorageClient, object_key: str) -> None:
        self._storage = storage
        self._object_key = object_key

    async def probe(self) -> WatchProbe | None:
        try:
            stat = await self._storage.stat(self._object_key)
        except StorageNotFoundError:
            return None
        except StorageError:
            logger.warning(
                "canvas watch probe failed: %s", self._object_key, exc_info=True
            )
            return None

        return WatchProbe(
            revision=f"{stat.size}:{stat.revision}", size=stat.size, closed=True
        )

    def attach_waker(self) -> asyncio.Event | None:
        return None


class StorageHashSource(WatchSource):
    """Слежение за маленьким текстовым файлом по содержимому.

    Спека диаграммы может меняться без смены размера, поэтому revision — хэш
    текста. Отказ чтения (файл переписывается) пропускает тик, а не гасит
    слежение.
    """

    def __init__(self, read: Callable[[], Awaitable[str]]) -> None:
        self._read = read
        self._last: WatchProbe | None = None

    async def probe(self) -> WatchProbe | None:
        try:
            text = await self._read()
        except RefusalError:
            return self._last

        raw = text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()

        self._last = WatchProbe(revision=digest, size=len(raw), closed=True)
        return self._last

    def attach_waker(self) -> asyncio.Event | None:
        return None


class PanelStorage:
    """Хранилище файлов панели: одна точка доступа к storage слоя данных."""

    @staticmethod
    def client() -> StorageClient:
        layer = get_data_layer()
        if not isinstance(layer, AttachmentDataLayer):
            msg = f"data layer does not address attachments: {type(layer)}"
            raise RuntimeError(msg)

        return layer.storage


class StorageWindows:
    """Окна текстового файла storage по смещению: память — одно окно.

    Та же математика стыков, что у журнала (WindowAlign): фронт склеивает
    окна встык без рваных строк. Файл в storage статичен на момент чтения,
    поэтому closed всегда истинно, а живость определяет слежение панели.
    """

    def __init__(self, storage: StorageClient, object_key: str) -> None:
        self._storage = storage
        self._object_key = object_key

    async def slice_at(self, offset: int) -> StreamSlice:
        """Окно вперёд от смещения; offset меньше нуля — хвост файла."""
        size = await self._size()

        if offset < 0:
            return await self.slice_before(size)

        start = max(0, min(offset, size))
        start, data = await self._aligned_read(
            start, min(JournalWindow.BYTES, size - start)
        )

        data, end = WindowAlign.forward_trim(start, data, size)

        return self._slice(data, start, end, size)

    async def slice_before(self, end: int) -> StreamSlice:
        """Окно, заканчивающееся ровно на end: стык для прокрутки вверх."""
        size = await self._size()

        stop = max(0, min(end, size))
        start = max(0, stop - JournalWindow.BYTES)
        start, data = await self._aligned_read(start, stop - start)

        return self._slice(data, start, start + len(data), size)

    async def _size(self) -> int:
        stat = await self._storage.stat(self._object_key)
        return stat.size

    async def _aligned_read(self, start: int, length: int) -> tuple[int, bytes]:
        if length <= 0:
            return start, b""

        read_start, read_length = WindowAlign.read_plan(start, length)
        raw = await self._read(read_start, read_length)

        return WindowAlign.head(start, raw)

    async def _read(self, start: int, length: int) -> bytes:
        window = ReadWindow(offset=start, length=length)

        collected = bytearray()
        async with await self._storage.open_stream(self._object_key, window) as body:
            async for chunk in body.chunks:
                collected.extend(chunk)

        return bytes(collected)

    def _slice(self, data: bytes, start: int, end: int, size: int) -> StreamSlice:
        return StreamSlice(
            text=data.decode("utf-8", errors="replace"),
            offset=start,
            end=end,
            size=size,
            window=int(JournalWindow.BYTES),
            closed=True,
            note="",
        )


class FileViewer(CanvasViewer):
    """База вьюверов: файл описывается ссылкой на storage, а не телом в памяти.

    Обновление файла ловит слежение панели по размеру: фронт перезагружает
    содержимое по той же ссылке.
    """

    LINK_ELEMENT: ClassVar[str] = "CanvasLink"
    """Имя jsx-компонента ссылки: public/elements/CanvasLink.jsx."""

    kind: ClassVar[CanvasKind] = CanvasKind.NOTICE
    suffixes: ClassVar[frozenset[str]] = frozenset()

    def handles(self, name: str) -> bool:
        lowered = name.lower()

        return any(lowered.endswith(suffix) for suffix in self.suffixes)

    def describe(self, key: ObjectKey, **extra: Any) -> CanvasContent:
        """Описание файла: путь, подпись, ссылка на файл и метка показа.

        Единственное место, где содержимое панели получает ссылку, — поэтому
        файл, показанный любым наследником, всегда можно скачать. Наследник
        добавляет своё (текст, окно потока, объяснение) через extra.
        """
        return CanvasContent(
            kind=self.kind,
            path=key.in_workspace(),
            label=key.name,
            url=self._serve(key),
            mime=self._mime(key.name),
            nonce=str(uuid.uuid4()),
            **extra,
        )

    async def content(self, key: ObjectKey) -> CanvasContent:
        return self.describe(key)

    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        described = await self.content(key)
        await push(described)

        link = CustomElementResult(
            element=self.LINK_ELEMENT,
            props={"path": key.in_workspace(), "label": key.name},
            title=key.name,
        )
        return OpenedCanvas(
            label=key.name,
            path=key.in_workspace(),
            nonce=described.nonce,
            link=link,
        )

    def watch_source(self, key: ObjectKey) -> WatchSource | None:
        return StorageStatSource(PanelStorage.client(), key.render())

    @staticmethod
    def _serve(key: ObjectKey) -> str:
        """Ссылка на файл панели: тред, каталог и имя; пользователь — из токена.

        Сессионная ссылка тут не годится: панель рассылается во все вкладки
        треда и переживает переподключение, а запись файла в памяти сессии —
        нет, и картинка ломалась бы молча.
        """
        return CanvasFileUrl.path(key)

    @staticmethod
    def _mime(name: str) -> str:
        guessed = mimetypes.guess_type(name)[0]
        if guessed:
            return guessed
        return "application/octet-stream"


class ImageViewer(FileViewer):
    """Картинки: главный формат графиков, которые рисуют bash/python-тулы."""

    kind: ClassVar[CanvasKind] = CanvasKind.IMAGE
    suffixes: ClassVar[frozenset[str]] = frozenset(
        {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    )


class PdfViewer(FileViewer):
    """PDF: встроенный просмотрщик браузера."""

    kind: ClassVar[CanvasKind] = CanvasKind.PDF
    suffixes: ClassVar[frozenset[str]] = frozenset({".pdf"})


class VideoViewer(FileViewer):
    """Видео: плеер браузера."""

    kind: ClassVar[CanvasKind] = CanvasKind.VIDEO
    suffixes: ClassVar[frozenset[str]] = frozenset({".mp4", ".webm", ".mov"})


class AudioViewer(FileViewer):
    """Аудио: плеер браузера."""

    kind: ClassVar[CanvasKind] = CanvasKind.AUDIO
    suffixes: ClassVar[frozenset[str]] = frozenset(
        {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
    )


class MarkdownViewer(FileViewer):
    """Markdown: формат требует документ целиком, фронт читает его по ссылке."""

    kind: ClassVar[CanvasKind] = CanvasKind.TEXT
    suffixes: ClassVar[frozenset[str]] = frozenset({".md"})


class LogViewer(FileViewer):
    """Логи и плоский текст: окна по смещению, файл целиком не читается."""

    kind: ClassVar[CanvasKind] = CanvasKind.STREAM
    suffixes: ClassVar[frozenset[str]] = frozenset({".txt", ".log"})

    async def content(self, key: ObjectKey) -> CanvasContent:
        windows = StorageWindows(PanelStorage.client(), key.render())

        try:
            piece = await windows.slice_at(0)
        except StorageNotFoundError as e:
            raise CanvasError(
                CanvasErrorKind.FILE_NOT_FOUND,
                f"file not found: {key.in_workspace()}",
            ) from e
        except StorageError as e:
            raise CanvasError(
                CanvasErrorKind.BAD_FILE,
                f"cannot read the file: {key.in_workspace()}: {e}",
            ) from e

        return self.describe(
            key,
            text=piece.text,
            note=StreamNote.status_of(piece),
            stream=StreamPos(
                offset=piece.offset,
                end=piece.end,
                size=piece.size,
                window=piece.window,
                closed=piece.closed,
            ),
        )


class CanvasPanel:
    """Панель справа от чата: содержимое одного файла, выбранного из чата.

    Каждый показ уходит под новым key: панель держит свой снапшот элементов
    и с прежним key его не перечитывает. Открытие ставит слежение за файлом.
    """

    TITLE: ClassVar[str] = "canvas"
    """Магическое имя: фронт chainlit включает по нему canvas-режим панели."""

    CONTENT_ID: ClassVar[str] = "boba-canvas-content"
    """Один id на слот содержимого: React обновляет элемент, а не пересоздаёт —
    панель не мигает при смене файла, а вьюверы не теряют своё состояние."""

    VIEW_ELEMENT: ClassVar[str] = "CanvasView"
    """Единственный компонент панели: рисует любое содержимое по его kind.

    Один компонент на все типы позволяет менять файл, не подменяя элемент:
    фронт сам забирает следующее содержимое, и панель не открывается заново.
    """

    @classmethod
    async def open(cls, key: ObjectKey) -> OpenedCanvas:
        viewer = CanvasRegistry.viewer_for(key.name)
        if viewer is None:
            await cls._explain(key)
            hint = CanvasRegistry.suffixes_hint()
            msg = f"no canvas viewer for {key.in_workspace()}; supported: {hint}"
            raise CanvasError(CanvasErrorKind.NO_VIEWER, msg)

        opened = await viewer.open(key, cls._push)
        cls.watch_file(key, viewer, opened.nonce)

        return opened

    @classmethod
    def watch_file(cls, key: ObjectKey, viewer: CanvasViewer, nonce: str) -> None:
        """Слежение за файлом панели; вьювер без источника не отслеживается."""
        if not CanvasWatch.enabled():
            return

        source = viewer.watch_source(key)
        if source is None:
            return

        CanvasWatch.show(key.thread_id, key.in_workspace(), nonce, source)

    @classmethod
    async def show(cls, content: CanvasContent) -> None:
        """Показ готового содержимого без вьювера: журнал вызова инструмента."""
        await cls._push(content)

    @classmethod
    def notice(cls, key: ObjectKey) -> CanvasContent:
        """Объяснение вместо содержимого: файл показать некому.

        Ссылка на файл остаётся: показать формат панель не умеет, а отдать
        его пользователю на скачивание — вполне.
        """
        return CanvasContent(
            kind=CanvasKind.NOTICE,
            path=key.in_workspace(),
            label=key.name,
            url=CanvasFileUrl.path(key),
            note=(
                "The panel cannot display this file format. Supported: "
                f"{CanvasRegistry.suffixes_hint()}"
            ),
        )

    @classmethod
    async def _explain(cls, key: ObjectKey) -> None:
        """Файл некому показать: в панели остаётся объяснение, а не прошлый файл."""
        await cls._push(cls.notice(key))

    @classmethod
    async def _push(cls, content: CanvasContent) -> None:
        """Открывает панель message-элементом с display='side'.

        chainlit сам держит side view по таким элементам: пока элемент цел,
        MessagesContainer не закрывает панель на каждый новый ход (в отличие от
        ElementSidebar, который он сбрасывает, когда среди элементов сообщения
        нет ни одного 'side'). Заголовок 'canvas' выставляется отдельно —
        MessagesContainer взял бы под него имя компонента, а по 'canvas' фронт
        включает полноэкранный canvas-режим панели.
        """
        element = cl.CustomElement(
            name=cls.VIEW_ELEMENT, props=content.props(), display="side"
        )
        element.id = cls.CONTENT_ID
        await element.send(for_id="")
        await cl.ElementSidebar.set_title(cls.TITLE)


class StreamShowRequest(BaseModel):
    """Payload действия canvas_stream: вызов, канал и способ доставки.

    Канал по умолчанию — stdout тела: его пишет каждый песочный вызов.
    Закрытый канал отвергается разбором — читать его фронт не может.

    inline — панель уже открыта и сама подменит содержимое: описание едет
    ответом на действие, а не элементом. Пуш элемента chainlit перемонтирует
    боковую панель целиком, с анимацией открытия и потерей состояния
    вьювера, поэтому им открывают панель, а не переключают канал в открытой.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str
    channel: JournalChannel = ToolChannel.STDOUT
    inline: bool = False

    @field_validator("channel")
    @classmethod
    def _readable(cls, value: JournalChannel) -> JournalChannel:
        if not JournalChannels.visible(value):
            msg = f"channel is not readable: {value.value}"
            raise ValueError(msg)

        return value


class StreamWindowRequest(BaseModel):
    """Payload действия canvas_stream_window: цель и одна из границ окна.

    path — показанный в панели путь: stream://{call_id}/{channel} для канала
    журнала или путь workspace для файла; канал берётся из него, а не из
    отдельного поля — иначе окно и показ разъехались бы по каналам. offset —
    окно вперёд от смещения (offset меньше нуля — хвост файла); before —
    окно, заканчивающееся на смещении. Ровно одно из двух.
    """

    model_config = ConfigDict(extra="ignore")

    path: str
    offset: int | None = None
    before: int | None = None

    @model_validator(mode="after")
    def _one_bound(self) -> Self:
        if (self.offset is None) == (self.before is None):
            msg = "canvas_stream_window: exactly one of offset and before"
            raise ValueError(msg)

        return self


class StreamLeaveRequest(BaseModel):
    """Payload действия canvas_leave: с какого показа ушла панель."""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    nonce: str = ""


class StreamActions:
    """Действия фронта над потоком: показ журнала, окна и уход с файла."""

    @staticmethod
    async def show(
        user_id: str, thread_id: str, payload: Mapping[str, object]
    ) -> dict[str, Any]:
        """Кнопка на шаге или вкладка канала: журнал в панель плюс слежение.

        Открытая панель просит содержимое ответом (inline) и подменяет его у
        себя; иначе панель открывается пушем элемента.
        """
        request = StreamShowRequest.model_validate(payload)
        stream_path = StreamPath(call_id=request.call_id, channel=request.channel)

        piece = ToolStreams.recorded_slice(
            user_id, thread_id, request.call_id, offset=0, channel=request.channel
        )
        if piece is None:
            logger.info(
                "stream show: no journal (hub=%s) user=%s thread=%s call=%s ch=%s",
                StreamJournalHub.get() is not None,
                user_id,
                thread_id,
                request.call_id,
                request.channel.value,
            )
            gone = StreamActions._gone(stream_path.render())
            if request.inline:
                return gone.props()

            await CanvasPanel.show(gone)
            return {}

        channels = ToolStreams.recorded_channels(user_id, thread_id, request.call_id)

        content = StreamActions.content(
            thread_id, stream_path, "", piece, str(uuid.uuid4()), channels
        )
        if not request.inline:
            await CanvasPanel.show(content)

        StreamActions._watch(user_id, thread_id, request, content, piece)

        if not request.inline:
            return {}

        return content.props()

    @staticmethod
    def _watch(
        user_id: str,
        thread_id: str,
        request: StreamShowRequest,
        content: CanvasContent,
        piece: StreamSlice,
    ) -> None:
        journal = StreamJournalHub.get()
        if journal is None:
            return

        try:
            key = StreamKey(
                user_id=user_id, thread_id=thread_id, call_id=request.call_id
            )
        except ValidationError:
            logger.warning("stream watch refused: %s", request.call_id, exc_info=True)
            return

        live = ToolStreams.get(thread_id, request.call_id)
        source = JournalWatchSource(journal, key, request.channel, live)
        seen = f"{piece.size}:{int(piece.closed)}"

        CanvasWatch.show(thread_id, content.path, content.nonce, source, seen)

    @staticmethod
    async def window(
        user_id: str, thread_id: str, payload: Mapping[str, object]
    ) -> dict[str, Any]:
        """Окно по границе: канал журнала или текстовый файл workspace."""
        request = StreamWindowRequest.model_validate(payload)

        stream_path = StreamPath.parse(request.path)
        if stream_path is not None:
            return StreamActions._journal_window(
                user_id, thread_id, stream_path, request
            )

        if StreamPath.is_stream(request.path):
            logger.warning("stream window: channel is not readable: %s", request.path)
            return {}

        return await StreamActions._file_window(user_id, thread_id, request)

    @staticmethod
    def _journal_window(
        user_id: str,
        thread_id: str,
        stream_path: StreamPath,
        request: StreamWindowRequest,
    ) -> dict[str, Any]:
        call_id = stream_path.call_id
        channel = stream_path.channel

        if request.before is not None:
            piece = ToolStreams.recorded_slice_before(
                user_id, thread_id, call_id, end=request.before, channel=channel
            )
        else:
            offset = request.offset
            if offset is None:
                offset = 0
            piece = ToolStreams.recorded_slice(
                user_id, thread_id, call_id, offset=offset, channel=channel
            )

        if piece is None:
            return {}

        # вкладки едут с показом: окно двигает только текст и координаты
        content = StreamActions.content(thread_id, stream_path, "", piece, "", ())
        return content.props()

    @staticmethod
    async def _file_window(
        user_id: str, thread_id: str, request: StreamWindowRequest
    ) -> dict[str, Any]:
        try:
            key = ObjectKey.from_workspace(user_id, thread_id, request.path)
        except ValueError:
            logger.warning("stream window: bad path %s", request.path)
            return {}

        windows = StorageWindows(PanelStorage.client(), key.render())

        try:
            if request.before is not None:
                piece = await windows.slice_before(request.before)
            else:
                offset = request.offset
                if offset is None:
                    offset = 0
                piece = await windows.slice_at(offset)
        except (StorageError, StorageNotFoundError):
            logger.warning("stream window read failed: %s", request.path, exc_info=True)
            return {}

        content = CanvasContent(
            kind=CanvasKind.STREAM,
            path=key.in_workspace(),
            label=key.name,
            url=CanvasFileUrl.path(key),
            text=piece.text,
            note=StreamNote.status_of(piece),
            stream=StreamActions._pos(piece),
        )
        return content.props()

    @staticmethod
    def leave(thread_id: str, payload: Mapping[str, object]) -> None:
        """Панель закрыта или ушла с файла: слежение этого показа снимается."""
        request = StreamLeaveRequest.model_validate(payload)

        CanvasWatch.leave(thread_id, request.nonce)

    @staticmethod
    def content(  # noqa: PLR0913
        thread_id: str,
        stream_path: StreamPath,
        label: str,
        piece: StreamSlice,
        nonce: str,
        channels: Sequence[JournalChannel],
    ) -> CanvasContent:
        names: list[str] = []
        for channel in channels:
            names.append(channel.value)

        return CanvasContent(
            kind=CanvasKind.STREAM,
            path=stream_path.render(),
            label=label,
            url=StreamUrl.path(thread_id, stream_path.call_id, stream_path.channel),
            text=piece.text,
            note=StreamNote.status_of(piece),
            nonce=nonce,
            stream=StreamActions._pos(piece),
            channel=stream_path.channel.value,
            channels=tuple(names),
        )

    @staticmethod
    def _pos(piece: StreamSlice) -> StreamPos:
        return StreamPos(
            offset=piece.offset,
            end=piece.end,
            size=piece.size,
            window=piece.window,
            closed=piece.closed,
        )

    @staticmethod
    def _gone(path: str) -> CanvasContent:
        return CanvasContent(
            kind=CanvasKind.NOTICE,
            path=path,
            label="tool stream",
            note=str(StreamNote.GONE),
        )
