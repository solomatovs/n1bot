"""Вывод инструмента в канвасе: выбор канала, насос показа и перемотка.

Исходящие каналы стадий пишет журнал песочницы; панель их только читает —
окнами по ходу вызова и после него, целиком файл во фронт не поднимается.
Какой канал показать, решает объявленный формат продукта узла: текстовый
продукт читается из журнала tool_payload, узел без продукта — из tool_stdout,
бинарный отдаётся скачиванием. Живой вызов тянет насос: журнал будит
подписчика по мере записи, остальные каналы доступны скачиванием.

Ошибки: наружу ничего не выходит; недоступный журнал показывается в панели
объяснением, сбой чтения гасится с записью в лог.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.chainlit.domain.keys import StreamUrl
from boba.chainlit.rendering.canvas import (
    CanvasContent,
    CanvasKind,
    CanvasPanel,
    StreamPos,
)
from boba.sandbox.journal import (
    CallJournal,
    JournalError,
    StreamJournal,
    StreamJournalHub,
    StreamSlice,
)
from boba.toolkit.channels import Channel, StreamFormat, StreamKey
from boba.toolkit.workflow import StageContract

__all__ = [
    "CanvasChannel",
    "SidebarChannel",
    "StageTools",
    "StageView",
    "StreamAction",
    "StreamNote",
    "StreamScreen",
    "StreamShowRequest",
    "StreamTarget",
    "StreamWaker",
    "StreamWindowRequest",
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
    STAGE = "stage"
    CHANNEL = "channel"
    OFFSET = "offset"


class StreamNote(StrEnum):
    """Формулировки статусной строки окна потока."""

    RUNNING = "running…"
    FINISHED = "finished"
    GONE = (
        "No journaled output for this call: the tool has not started writing "
        "yet, or the log was rotated out of the volume."
    )
    BINARY = "This output is binary: download the file to see its content."

    @classmethod
    def status_of(cls, piece: StreamSlice) -> str:
        if not piece.closed:
            return str(cls.RUNNING)

        if piece.note:
            return piece.note

        return str(cls.FINISHED)


class StageView(StrEnum):
    """Как панель показывает продукт узла: канал журнала и способ показа."""

    PAYLOAD = "payload"
    """Текстовый продукт: журнал канала данных."""
    STDOUT = "stdout"
    """Продукта нет: общий вывод стадии."""
    DOWNLOAD = "download"
    """Бинарный продукт: показывать нечем, файл отдаётся скачиванием."""

    @property
    def channel(self) -> Channel:
        """Канал журнала, который открывает панель."""
        if self is StageView.STDOUT:
            return Channel.TOOL_STDOUT

        return Channel.TOOL_PAYLOAD

    @classmethod
    def of(cls, out: StreamFormat | None) -> StageView:
        """Объявленный формат продукта узла -> способ показа."""
        if out is None:
            return cls.STDOUT

        if out is StreamFormat.BYTES:
            return cls.DOWNLOAD

        return cls.PAYLOAD


class StageTools:
    """Инструменты со стадиями песочницы и способ показа продукта их узлов.

    Реестр стадий связывается с лентой при сборке инструментов: у шага
    инструмента со стадиями есть журнал, а контракт узла говорит, какой канал
    показывать. Стадия вне реестра — узел графа workflow или mount-группа —
    показывается общим выводом стадии.
    """

    _TOOLS: ClassVar[frozenset[str]] = frozenset()
    _VIEWS: ClassVar[Mapping[str, StageView]] = {}

    @classmethod
    def configure(
        cls, tools: Iterable[str], contracts: Mapping[str, StageContract]
    ) -> None:
        views: dict[str, StageView] = {}
        for stage, contract in contracts.items():
            views[stage] = StageView.of(contract.out)

        cls._TOOLS = frozenset(tools)
        cls._VIEWS = views

    @classmethod
    def journalled(cls, tool_name: str) -> bool:
        """Вызов инструмента идёт стадиями песочницы: у его шага есть журнал."""
        return tool_name in cls._TOOLS

    @classmethod
    def view_of(cls, stage: str) -> StageView:
        view = cls._VIEWS.get(stage)
        if view is None:
            return StageView.STDOUT

        return view

    @classmethod
    def reset(cls) -> None:
        """Сброс: пользуются тесты, приложению это не нужно."""
        cls._TOOLS = frozenset()
        cls._VIEWS = {}


class StreamTarget(BaseModel):
    """Канал вызова в показе панели: адрес журнала и способ показа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: StreamKey
    view: StageView


class StreamShowRequest(BaseModel):
    """Payload действия canvas_stream: вызов, адрес канала и режим показа.

    Адрес присылает панель, которая уже показывает канал; кнопка шага знает
    только вызов, и канал выбирает сервер. follow — к концу файла: живой вызов
    следит за хвостом насосом, завершённый показывает последнее окно.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str = Field(min_length=1)
    stage: str = ""
    channel: Channel | None = None
    follow: bool = False

    @model_validator(mode="after")
    def _whole_address(self) -> Self:
        if self.stage and self.channel is None:
            raise ValueError("canvas_stream: stage without channel")

        if self.channel is not None and not self.stage:
            raise ValueError("canvas_stream: channel without stage")

        return self


class StreamWindowRequest(BaseModel):
    """Payload действия canvas_stream_window: канал и одна из границ окна.

    offset — окно вперёд от смещения (прокрутка вниз); before — окно,
    заканчивающееся на смещении (прокрутка вверх). Ровно одно из двух.
    """

    model_config = ConfigDict(extra="ignore")

    call_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    channel: Channel
    offset: int | None = None
    before: int | None = None

    @model_validator(mode="after")
    def _one_bound(self) -> Self:
        forward = self.offset is not None
        backward = self.before is not None

        if forward and backward:
            raise ValueError("canvas_stream_window: offset and before together")

        if not forward and not backward:
            raise ValueError("canvas_stream_window: no window bound")

        return self


class CanvasChannel(Protocol):
    """Доставка содержимого потока в канвас; реализация выбирает транспорт."""

    @abstractmethod
    async def push(self, content: CanvasContent) -> None: ...


class SidebarChannel:
    """Канал через панель канваса: тот же путь, что у показа файлов."""

    async def push(self, content: CanvasContent) -> None:
        await CanvasPanel.show(content)


class StreamWaker:
    """Подписчик журнала на время показа: будит насос по записи в файлы вызова.

    on_data приходит из writer-потока журнала, а событие живёт в loop'е
    приложения, поэтому пробуждение идёт через call_soon_threadsafe.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()

    def clear(self) -> None:
        self._event.clear()

    async def wait(self) -> None:
        await self._event.wait()

    def on_open(self, call: CallJournal) -> None:
        """Открытие журнала показу не нужно: панель открывает уже записанное."""

    def on_close(self, call: CallJournal) -> None:
        if call.prefix != self._prefix:
            return

        self._wake()

    def on_data(self, key: StreamKey) -> None:
        if key.call_prefix != self._prefix:
            return

        self._wake()

    def _wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            logger.debug("stream wakeup after loop shutdown: %s", self._prefix)


class ToolStreams:
    """Доступ панели к журналу песочницы: живые вызовы, адреса и окна.

    Журнал ведёт песочница, UI только читает — реестр живых вызовов и файлы
    тома тут единственный источник. Отказ журнала выше не поднимается: панель
    показывает отсутствие данных, ход инструмента это не трогает.
    """

    TAIL: ClassVar[int] = -1
    """Смещение хвоста файла: окно, заканчивающееся на последнем байте."""

    @classmethod
    def target(
        cls, user_id: str, thread_id: str, request: StreamShowRequest
    ) -> StreamTarget | None:
        """Канал показа: адрес из запроса либо выбранный по контракту узла."""
        if not request.stage:
            return cls._picked(user_id, thread_id, request.call_id)

        if request.channel is None:
            return None

        key = cls.key_of(
            user_id, thread_id, request.call_id, request.stage, request.channel
        )
        if key is None:
            return None

        return StreamTarget(key=key, view=StageTools.view_of(key.stage))

    @classmethod
    def live_call(
        cls, user_id: str, thread_id: str, call_id: str
    ) -> CallJournal | None:
        """Живой вызов пользователя; чужой вызов живым для панели не считается."""
        journal = cls._journal()
        if journal is None:
            return None

        call = journal.registry.call_of(thread_id, call_id)
        if call is None:
            return None

        if call.context.user_id != user_id:
            return None

        return call

    @classmethod
    def key_of(
        cls, user_id: str, thread_id: str, call_id: str, stage: str, channel: Channel
    ) -> StreamKey | None:
        """Адрес журнала из полей запроса; небезопасный адрес — «нет данных»."""
        try:
            return StreamKey(
                user_id=user_id,
                thread_id=thread_id,
                call_id=call_id,
                stage=stage,
                channel=channel,
            )
        except ValidationError:
            logger.warning("unsafe stream address: %s", call_id, exc_info=True)
            return None

    @classmethod
    def slice_at(cls, key: StreamKey, offset: int) -> StreamSlice | None:
        """Окно журнала вперёд; любой отказ журнала — «нет данных», не сбой чата."""
        journal = cls._journal()
        if journal is None:
            return None

        try:
            return journal.slice_at(key, offset)
        except JournalError:
            logger.warning("stream journal read failed: %s", key.rel_log())
            return None

    @classmethod
    def slice_before(cls, key: StreamKey, end: int) -> StreamSlice | None:
        """Окно перед смещением: прокрутка вверх; отказ — «нет данных»."""
        journal = cls._journal()
        if journal is None:
            return None

        try:
            return journal.slice_before(key, end)
        except JournalError:
            logger.warning("stream journal read failed: %s", key.rel_log())
            return None

    @classmethod
    def drop_thread(cls, thread_id: str) -> None:
        """Конец хода: насос снимается, журнал закрывает песочница."""
        StreamScreen.leave(thread_id)

    @classmethod
    def _picked(
        cls, user_id: str, thread_id: str, call_id: str
    ) -> StreamTarget | None:
        """Канал вызова для показа: последний записанный из показуемых."""
        journal = cls._journal()
        if journal is None:
            return None

        try:
            keys = journal.keys_of(user_id, thread_id, call_id)
        except JournalError:
            logger.warning("stream journal listing failed: %s", call_id)
            return None

        target: StreamTarget | None = None
        for key in keys:
            view = StageTools.view_of(key.stage)
            if key.channel is not view.channel:
                continue

            target = StreamTarget(key=key, view=view)

        return target

    @classmethod
    def _journal(cls) -> StreamJournal | None:
        try:
            return StreamJournalHub.get()
        except JournalError:
            logger.warning("stream journal is not configured")
            return None


class StreamScreen:
    """Насос показа: один поток на тред, переключение снимает предыдущий."""

    SCHEME: ClassVar[str] = "stream://"
    """Псевдо-путь панели: по нему фронт отличает поток от файла."""

    COALESCE_SEC: ClassVar[float] = 0.3
    """Пауза после пробуждения: болтливый инструмент не заливает сокет."""

    VANISHED: ClassVar[StreamSlice] = StreamSlice(
        text="",
        offset=0,
        end=0,
        size=0,
        window=0,
        closed=True,
        note=str(StreamNote.GONE),
    )
    """Файл журнала исчез из-под показа: вытеснен ротацией тома."""

    _PUMPS: ClassVar[dict[str, asyncio.Task[None]]] = {}
    _REVISIONS: ClassVar[itertools.count[int]] = itertools.count(1)
    """Сквозной номер показа: едет в nonce, по нему фронт видит смену props."""

    @classmethod
    async def show(
        cls, thread_id: str, target: StreamTarget, channel: CanvasChannel
    ) -> asyncio.Task[None]:
        cls.leave(thread_id)
        task = asyncio.create_task(cls._pump(target, channel))
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
        key: StreamKey,
        piece: StreamSlice,
        channel: CanvasChannel,
        follow: bool,
    ) -> None:
        """Показ окна записанного журнала: без насоса, один кадр."""
        await channel.push(cls.content(key, piece, follow))

    @classmethod
    async def gone(cls, call_id: str, channel: CanvasChannel) -> None:
        """Журнала нет: панель объясняет это вместо содержимого."""
        content = CanvasContent(
            kind=CanvasKind.NOTICE,
            path=f"{cls.SCHEME}{call_id}",
            label="tool output",
            note=str(StreamNote.GONE),
        )
        await channel.push(content)

    @classmethod
    async def binary(cls, key: StreamKey, channel: CanvasChannel) -> None:
        """Продукт узла бинарный: панель даёт ссылку вместо содержимого."""
        content = CanvasContent(
            kind=CanvasKind.NOTICE,
            path=cls.path_of(key),
            label=cls.label_of(key),
            url=StreamUrl.path(key),
            note=str(StreamNote.BINARY),
        )
        await channel.push(content)

    @classmethod
    def content(
        cls, key: StreamKey, piece: StreamSlice, follow: bool
    ) -> CanvasContent:
        return CanvasContent(
            kind=CanvasKind.STREAM,
            path=cls.path_of(key),
            label=cls.label_of(key),
            url=StreamUrl.path(key),
            text=piece.text,
            note=StreamNote.status_of(piece),
            nonce=str(next(cls._REVISIONS)),
            stream=StreamPos(
                call_id=key.call_id,
                stage=key.stage,
                channel=key.channel,
                offset=piece.offset,
                end=piece.end,
                size=piece.size,
                window=piece.window,
                closed=piece.closed,
                follow=follow,
            ),
        )

    @classmethod
    def path_of(cls, key: StreamKey) -> str:
        """Псевдо-путь канала: адрес показа для фронта."""
        return f"{cls.SCHEME}{key.call_id}/{key.stage}/{key.channel.value}"

    @classmethod
    def label_of(cls, key: StreamKey) -> str:
        """Подпись окна: стадия и её канал."""
        return f"{key.stage}.{key.channel.value}"

    @classmethod
    def _forget(cls, thread_id: str, task: asyncio.Task[None]) -> None:
        if cls._PUMPS.get(thread_id) is task:
            del cls._PUMPS[thread_id]

    @classmethod
    async def _pump(cls, target: StreamTarget, channel: CanvasChannel) -> None:
        """Хвост журнала на каждое пробуждение; закрытый пушится последний раз."""
        try:
            journal = StreamJournalHub.get()
        except JournalError:
            logger.warning("stream pump did not start: journal is not configured")
            return

        waker = StreamWaker(target.key.call_prefix)
        journal.registry.subscribe(waker)

        try:
            await cls._follow(journal, target.key, channel, waker)
        except asyncio.CancelledError:
            raise
        except JournalError:
            logger.warning("stream pump stopped: journal read failed", exc_info=True)
        except Exception:
            logger.warning("stream pump stopped: push failed", exc_info=True)
        finally:
            journal.registry.unsubscribe(waker)

    @classmethod
    async def _follow(
        cls,
        journal: StreamJournal,
        key: StreamKey,
        channel: CanvasChannel,
        waker: StreamWaker,
    ) -> None:
        while True:
            waker.clear()

            piece = journal.slice_at(key, ToolStreams.TAIL)
            if piece is None:
                await channel.push(cls.content(key, cls.VANISHED, follow=True))
                return

            await channel.push(cls.content(key, piece, follow=True))

            if piece.closed:
                return

            await waker.wait()
            await asyncio.sleep(cls.COALESCE_SEC)


async def show_stream_action(
    user_id: str, thread_id: str, payload: Mapping[str, object]
) -> None:
    """Показ канала вызова: по умолчанию окно с начала файла; follow — к концу,
    у живого вызова с насосом-слежением за хвостом."""
    request = StreamShowRequest.model_validate(payload)
    channel = SidebarChannel()

    target = ToolStreams.target(user_id, thread_id, request)
    if target is None:
        StreamScreen.leave(thread_id)
        await StreamScreen.gone(request.call_id, channel)
        return

    if target.view is StageView.DOWNLOAD:
        StreamScreen.leave(thread_id)
        await StreamScreen.binary(target.key, channel)
        return

    if request.follow:
        live = ToolStreams.live_call(user_id, thread_id, request.call_id)
        if live is not None:
            await StreamScreen.show(thread_id, target, channel)
            return

    StreamScreen.leave(thread_id)

    offset = 0
    if request.follow:
        offset = ToolStreams.TAIL

    piece = ToolStreams.slice_at(target.key, offset)
    if piece is None:
        await StreamScreen.gone(request.call_id, channel)
        return

    await StreamScreen.recorded(target.key, piece, channel, follow=request.follow)


async def window_stream_action(
    user_id: str, thread_id: str, payload: Mapping[str, object]
) -> dict[str, Any]:
    """Прокрутка: окно журнала по границе; уход из хвоста снимает насос."""
    request = StreamWindowRequest.model_validate(payload)

    StreamScreen.leave(thread_id)

    key = ToolStreams.key_of(
        user_id, thread_id, request.call_id, request.stage, request.channel
    )
    if key is None:
        return {}

    if request.before is not None:
        piece = ToolStreams.slice_before(key, request.before)
    else:
        offset = request.offset
        if offset is None:
            offset = 0
        piece = ToolStreams.slice_at(key, offset)

    if piece is None:
        return {}

    content = StreamScreen.content(key, piece, follow=False)

    return content.props()
