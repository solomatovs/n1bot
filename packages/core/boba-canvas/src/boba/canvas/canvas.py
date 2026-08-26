"""Канвас: виды окон, вердикты рендера, реестр открытых окон, слежение за источником,
сигналы фронту, вьюверы файлов; порты доставки и транспорта сигналов.

Ошибки:
CanvasError — окно нельзя открыть или обновить; код — CanvasErrorKind.
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
    field_validator,
    model_validator,
)

from boba.canvas.keys import ObjectKey
from boba.identity.errors import RefusalError
from boba.toolkit.channels import JournalChannel, JournalChannels, ToolChannel
from boba.toolkit.result import CustomElementResult, DiagramResult

__all__ = [
    "CanvasAction",
    "CanvasContent",
    "CanvasError",
    "CanvasErrorKind",
    "CanvasKind",
    "CanvasPush",
    "CanvasRegistry",
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
