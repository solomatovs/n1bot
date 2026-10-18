"""Канвас: виды окон, вердикты рендера, реестр открытых окон, слежение за источником,
сигналы фронту, вьюверы файлов; порты доставки и транспорта сигналов.

Ошибки:
CanvasError — окно нельзя открыть или обновить; код — CanvasErrorKind.
WatchProbeError — источник слежения не снял состояние файла сейчас; его
    ловит сторож CanvasWatch и повторяет опрос на следующем такте.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from boba.canvas.keys import ObjectKey
from boba.canvas.transfer import ContentDisposition
from boba.identity.errors import RefusalError
from boba.toolkit.channels import (
    JournalChannels,
    ReadableChannel,
    ToolChannel,
)
from boba.toolkit.result import VisualResult

__all__ = [
    "CanvasAction",
    "CanvasContent",
    "CanvasElement",
    "CanvasError",
    "CanvasErrorKind",
    "CanvasFileRequest",
    "CanvasHoldRequest",
    "CanvasKind",
    "CanvasPush",
    "CanvasRegistry",
    "CanvasScope",
    "CanvasSignal",
    "CanvasViewer",
    "CanvasWatch",
    "OpenedCanvas",
    "RenderReport",
    "RenderStatus",
    "RenderVerdict",
    "RenderVerdicts",
    "SignalTransport",
    "StreamLeaveRequest",
    "StreamPath",
    "StreamPos",
    "StreamShowRequest",
    "StreamWindowRequest",
    "WatchProbe",
    "WatchProbeError",
    "WatchSource",
]

logger = logging.getLogger(__name__)


class CanvasErrorKind(StrEnum):
    """Коды отказов панели и тулов канваса: уезжают в ErrorResult.error_kind."""

    BAD_PATH = "bad_path"
    NO_VIEWER = "no_canvas_viewer"
    FILE_NOT_FOUND = "file_not_found"
    BAD_FILE = "bad_file"
    TOO_LARGE = "file_too_large"
    RENDER_FAILED = "render_failed"
    INVALID_SPEC = "invalid_diagram_spec"


class CanvasError(RefusalError):
    """Панель не показала файл; kind — код причины, текст готов для LLM."""


class WatchProbeError(RuntimeError):
    """Источник слежения не снял состояние файла сейчас: хранилище или
    сервер журнала отказали, файла нет или он переписывается. Бросает
    источник (WatchSource.probe), ловит сторож CanvasWatch: отказ уходит в
    лог, а опрос повторяется на следующем такте. Текст называет файл и
    причину нижнего слоя."""


@dataclass(frozen=True, slots=True)
class CanvasScope:
    """Чьи файлы показывает панель: пользователь и тред. Строят его лента
    (ChatView), обработчики действий панели по сессии чата и ретранслятор по
    регистрации сервера; key переводит путь workspace в ключ файла этой
    области."""

    user_id: str
    thread_id: str

    def key(self, path: str) -> ObjectKey:
        """Ключ файла по пути в workspace треда.

        Ошибки:
        CanvasError — путь вне каталогов треда (BAD_PATH).
        """
        try:
            return ObjectKey.from_workspace(self.user_id, self.thread_id, path)
        except ValueError as exc:
            raise CanvasError(CanvasErrorKind.BAD_PATH, str(exc)) from exc


class CanvasAction(StrEnum):
    """Действия фронта для канваса и поля их payload."""

    OPEN = "canvas_open"
    CONTENT = "canvas_content"
    STATUS = "canvas_render_status"
    SHOW = "canvas_stream"
    WINDOW = "canvas_stream_window"
    HOLD = "canvas_hold"
    LEAVE = "canvas_leave"
    CALL_ID = "call_id"


class CanvasElement(StrEnum):
    """Имена элементов ленты и канваса: jsx-компоненты public/elements."""

    STREAM = "CanvasStream"
    """Кнопка журнала вызова; идентификатор вызова — в props (CALL_ID)."""
    LINK = "CanvasLink"
    """Ссылка на файл workspace в ленте: клик открывает его в панели."""
    VIEW = "CanvasView"
    """Единственный компонент панели: рисует любое содержимое по его kind.

    Один компонент на все типы позволяет менять файл, не подменяя элемент:
    фронт сам забирает следующее содержимое, и панель не открывается заново.
    """
    JSON_TREE = "JsonTree"
    """Дерево JSON ответа инструмента; значение — в props (value)."""


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
    """Ожидания вердиктов рендера: nonce показа -> future с исходом."""

    _WAITERS: ClassVar[dict[str, asyncio.Future[RenderVerdict]]] = {}

    @classmethod
    def expect(cls, nonce: str) -> None:
        """Начать ждать вердикт; зовётся до показа элемента, чтобы не было гонки."""
        loop = asyncio.get_running_loop()
        cls._WAITERS[nonce] = loop.create_future()

    @classmethod
    def report(cls, payload: Mapping[str, object]) -> None:
        """Принять отчёт браузера; битый payload — ValidationError наверх."""
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

    @property
    def disposition(self) -> ContentDisposition:
        """Как отдавать файл вида ссылкой: открыть в браузере можно только
        видео и звук — их браузер показывает сам и без скриптов; pdf рисует
        pdf.js на странице чата, а прочее — html, svg, текст — скачивается,
        чтобы не исполниться в origin чата."""
        match self:
            case CanvasKind.VIDEO | CanvasKind.AUDIO:
                return ContentDisposition.INLINE
            case _:
                return ContentDisposition.ATTACHMENT


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
    """Описание содержимого панели: что рисовать и откуда взять."""

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
    revision: str = ""
    """Версия файла, которую видел сторож при показе: с неё панель держит
    показ (canvas_hold); пусто — версия неизвестна."""
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
    """Открытый в панели файл: подпись, путь и метка показа."""

    label: str
    path: str
    nonce: str


class CanvasPush(Protocol):
    """Показ содержимого в панели; повторный вызов заменяет его."""

    @abstractmethod
    async def __call__(self, content: CanvasContent) -> None: ...


class WatchProbe(BaseModel):
    """Состояние наблюдаемого файла: по смене revision фронту уходит сигнал."""

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
        """Текущее состояние; None — источник исчерпан навсегда (журнал
        вызова забыт), следить не за чем.

        Ошибки:
        WatchProbeError — состояние не снять сейчас; сторож повторит опрос.
        """
        ...

    @abstractmethod
    def attach_waker(self) -> asyncio.Event | None:
        """Будильник записи в текущем loop'е; None — источник только поллится."""
        ...

    @abstractmethod
    def detach_waker(self, event: asyncio.Event) -> None:
        """Снимает будильник, выданный attach_waker; источнику без будильника
        снимать нечего.
        """
        ...


class CanvasViewer(Protocol):
    """Файлы одного вида: какие MIME умеет и чем их описать."""

    kind: ClassVar[CanvasKind]
    """Чем фронт рисует файлы вьювера."""

    mimes: ClassVar[frozenset[str]]
    """MIME вьювера: точный тип или семейство с '/' на конце ('image/');
    панель перечисляет их, когда файл показать некому."""

    @abstractmethod
    def handles(self, mime: str) -> bool:
        """Берётся ли вьювер за файл с таким MIME."""
        ...

    @abstractmethod
    async def open(self, key: ObjectKey, mime: str, push: CanvasPush) -> OpenedCanvas:
        """Показать файл key вида mime через push; вернуть подпись, путь и
        метку показа."""
        ...

    @abstractmethod
    async def link(self, key: ObjectKey, mime: str) -> VisualResult:
        """Ссылка на файл вида mime в ленте, без показа в панели: её рисует
        итог вызова и вживую, и при воспроизведении истории; клик по ней
        открывает файл с тем же mime."""
        ...

    @abstractmethod
    async def content(self, key: ObjectKey, mime: str) -> CanvasContent:
        """Описание файла вида mime для панели; панель его только доставляет."""
        ...

    @abstractmethod
    def watch_source(self, key: ObjectKey) -> WatchSource | None:
        """Источник слежения за файлом; None — за файлом не следят."""
        ...


class CanvasRegistry:
    """Вьюверы канваса: MIME файла -> кто его рисует; порядок регистрации
    решает спор."""

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
    def viewer_for(cls, mime: str) -> CanvasViewer | None:
        """Вьювер файла с MIME mime; None — показать его некому."""
        for viewer in cls._VIEWERS:
            if viewer.handles(mime):
                return viewer
        return None

    @classmethod
    def disposition(cls, mime: str) -> ContentDisposition:
        """Как отдавать по ссылке файл вида mime: показ браузером — только
        если его вьювер рисует средствами браузера, иначе скачивание."""
        viewer = cls.viewer_for(mime)
        if viewer is None:
            return ContentDisposition.ATTACHMENT

        return viewer.kind.disposition

    @classmethod
    def mimes_hint(cls) -> str:
        """Какие MIME панель показывает — для пользователя и LLM."""
        known: set[str] = set()
        for viewer in cls._VIEWERS:
            known.update(viewer.mimes)

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
    async def send(self, thread_id: str, signal: CanvasSignal) -> None: ...


class CanvasSignal(BaseModel):
    """Сигнал фронту: показанный файл изменился, содержимое не едет."""

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
    """Слежение за показами панели: свой сторож на каждый показ (метку nonce)
    треда сообщает фронту об изменениях файла.

    Показ держат экземпляры панели во вкладках (holder — метка экземпляра):
    экземпляр объявляет показ при монтировании и после переподключения
    сокета (hold), а уходя с него, снимает свою метку (leave). Пересоздание
    элемента панели — это уход прежнего экземпляра и объявление нового, и
    они приходят в любом порядке, поэтому слежение снимается, только когда
    показ не держит никто. Сервер ставит сторожа при показе (show), и до
    объявления панели его не держит никто: такие сторожа треда снимает
    следующий показ. Сторож кончается и сам — у треда нет подключённых
    вкладок или источник исчерпан; вкладка, подключившись снова, объявляет
    показ и ставит сторожа заново. Отказ опроса (WatchProbeError) слежение
    не кончает: он уходит в лог, опрос повторяется на следующем такте.
    """

    POLL_SEC: ClassVar[float] = 1.0
    """Период опроса файловых источников и проверки живости треда."""

    COALESCE_SEC: ClassVar[float] = 0.3
    """Пауза после пробуждения: болтливый инструмент не заливает сокет."""

    _TRANSPORT: ClassVar[SignalTransport | None] = None
    _WATCHES: ClassVar[dict[tuple[str, str], CanvasWatch]] = {}

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
        self._key = (thread_id, nonce)
        self._source = source
        self._seen = seen
        self._holders: set[str] = set()
        self._failures = 0
        self._task: asyncio.Task[None] | None = None

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
        for thread_id, _ in list(cls._WATCHES):
            cls.drop(thread_id)
        cls._TRANSPORT = None

    @classmethod
    def show(
        cls,
        thread_id: str,
        path: str,
        nonce: str,
        source: WatchSource,
        seen: str,
    ) -> None:
        """Следить за показом nonce файла path треда; seen — версия, которую
        показ отдал фронту, пустая — неизвестна, и сигнал шлёт первый же
        опрос. За уже отслеживаемым показом второй сторож не ставится;
        сторожа треда, которых не держит ни одна панель, снимаются."""
        if cls._TRANSPORT is None:
            return

        for key, known in list(cls._WATCHES.items()):
            if key[0] != thread_id:
                continue

            if known._holders:
                continue

            if known._nonce == nonce:
                continue

            cls._drop(key)

        if (thread_id, nonce) in cls._WATCHES:
            return

        watch = cls(thread_id, path, nonce, source, seen)
        cls._WATCHES[watch._key] = watch
        watch._task = asyncio.create_task(watch._run())

    @classmethod
    def hold(cls, thread_id: str, nonce: str, holder: str, seen: str) -> bool:
        """Экземпляр панели holder держит показ nonce и видит его версию seen.

        True — за показом следят, и версия новее seen дойдёт до панели
        сигналом; False — сторожа нет (не ставился или кончился), его ставит
        show.
        """
        watch = cls._WATCHES.get((thread_id, nonce))
        if watch is None:
            return False

        watch._holders.add(holder)
        watch._seen = seen
        return True

    @classmethod
    def leave(cls, thread_id: str, nonce: str, holder: str) -> None:
        """Экземпляр панели holder ушёл с показа nonce: слежение снимается,
        когда показ больше никто не держит; другие показы не трогаются."""
        key = (thread_id, nonce)
        watch = cls._WATCHES.get(key)
        if watch is None:
            return

        watch._holders.discard(holder)
        if watch._holders:
            return

        cls._drop(key)

    @classmethod
    def drop(cls, thread_id: str) -> None:
        """Снимает все сторожа треда."""
        for key in list(cls._WATCHES):
            if key[0] != thread_id:
                continue

            cls._drop(key)

    @classmethod
    def watching(cls, thread_id: str) -> tuple[str, ...]:
        """Пути показов треда под слежением в порядке постановки."""
        paths: list[str] = []
        for key, watch in cls._WATCHES.items():
            if key[0] != thread_id:
                continue

            paths.append(watch._path)

        return tuple(paths)

    @classmethod
    def _drop(cls, key: tuple[str, str]) -> None:
        watch = cls._WATCHES.pop(key, None)
        if watch is None:
            return

        if watch._task is not None:
            watch._task.cancel()

    async def _run(self) -> None:
        transport = self._TRANSPORT
        if transport is None:
            return

        waker = self._source.attach_waker()
        try:
            await self._follow(transport, waker)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "canvas watch of %s (show %s) in thread %s stopped by an "
                "unexpected error: %s",
                self._path,
                self._nonce,
                self._thread_id,
                exc,
                exc_info=True,
            )
        finally:
            if waker is not None:
                self._source.detach_waker(waker)

            self._forget()

    async def _follow(
        self, transport: SignalTransport, waker: asyncio.Event | None
    ) -> None:
        """Такты слежения, пока сторож нужен; причина конца — в лог."""
        while self._current():
            if not transport.alive(self._thread_id):
                self._ended("the thread has no connected tab")
                return

            try:
                probe = await self._source.probe()
            except WatchProbeError as exc:
                self._failed(exc)
                await self._pause(waker)
                continue

            self._recovered()
            if probe is None:
                self._ended("the source is exhausted")
                return

            final = await self._advance(transport, probe)
            if final:
                self._ended("the source is final")
                return

            await self._pause(waker)

    async def _advance(self, transport: SignalTransport, probe: WatchProbe) -> bool:
        """Сигнал на смену версии; True — источник исчерпан, сигнал дослан."""
        if probe.revision != self._seen:
            self._seen = probe.revision
            await transport.send(self._thread_id, self._signal(probe))

        return probe.final

    def _failed(self, exc: WatchProbeError) -> None:
        self._failures += 1
        if self._failures > 1:
            return

        logger.warning(
            "canvas watch of %s (show %s) in thread %s: probing failed, it is "
            "retried every %.1fs while the show is held: %s",
            self._path,
            self._nonce,
            self._thread_id,
            self.POLL_SEC,
            exc,
        )

    def _recovered(self) -> None:
        if not self._failures:
            return

        logger.info(
            "canvas watch of %s (show %s) in thread %s: probing recovered after "
            "%d failed attempts",
            self._path,
            self._nonce,
            self._thread_id,
            self._failures,
        )
        self._failures = 0

    def _ended(self, reason: str) -> None:
        logger.info(
            "canvas watch of %s (show %s) in thread %s ended: %s",
            self._path,
            self._nonce,
            self._thread_id,
            reason,
        )

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
        return self._WATCHES.get(self._key) is self

    def _forget(self) -> None:
        if self._current():
            del self._WATCHES[self._key]

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
    """Псевдо-путь панели для канала журнала: сборка и разбор в одном месте."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SCHEME: ClassVar[str] = "stream://"

    call_id: str
    channel: ReadableChannel

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


class CanvasFileRequest(BaseModel):
    """Payload действий canvas_open и canvas_content: файл workspace и его вид
    так, как их назвала ссылка; refresh — перечитать уже показанный файл, не
    перенося слежение."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1)
    mime: str = Field(min_length=1)
    refresh: bool = False


class StreamShowRequest(BaseModel):
    """Payload действия canvas_stream: вызов, канал и способ доставки."""

    model_config = ConfigDict(extra="ignore")

    call_id: str
    channel: ReadableChannel = ToolChannel.STDOUT
    inline: bool = False


class StreamWindowRequest(BaseModel):
    """Payload действия canvas_stream_window: цель и одна из границ окна."""

    model_config = ConfigDict(extra="ignore")

    path: str
    offset: int | None = None
    before: int | None = None

    @model_validator(mode="after")
    def _one_bound(self) -> Self:
        if (self.offset is None) == (self.before is None):
            msg = (
                f"canvas_stream_window for {self.path!r}: expected exactly one "
                f"of offset and before, got offset={self.offset!r}, "
                f"before={self.before!r}"
            )
            raise ValueError(msg)

        return self


class CanvasHoldRequest(BaseModel):
    """Payload действия canvas_hold: экземпляр панели holder держит показ
    nonce файла path вида mime и видит его версию revision. У журнала вызова
    вида нет (mime пуст), у показа с неизвестной версией пуст revision."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1)
    mime: str
    nonce: str = Field(min_length=1)
    holder: str = Field(min_length=1)
    revision: str


class StreamLeaveRequest(BaseModel):
    """Payload действия canvas_leave: с какого показа ушёл экземпляр панели
    holder."""

    model_config = ConfigDict(extra="ignore")

    nonce: str
    holder: str = Field(min_length=1)
