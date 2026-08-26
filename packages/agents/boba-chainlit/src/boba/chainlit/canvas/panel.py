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
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar

from pydantic import (
    ValidationError,
)

import chainlit as cl
from boba.canvas.canvas import (
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPush,
    CanvasRegistry,
    CanvasViewer,
    CanvasWatch,
    OpenedCanvas,
    StreamLeaveRequest,
    StreamPath,
    StreamPos,
    StreamShowRequest,
    StreamWindowRequest,
    WatchProbe,
    WatchSource,
)
from boba.canvas.journal import (
    JournalWindow,
    StreamJournalHub,
    StreamKey,
    StreamSlice,
    WindowAlign,
)
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import StorageClient
from boba.chainlit.domain.keys import CanvasFileUrl, StreamUrl
from boba.identity.errors import RefusalError
from boba.toolkit.channels import JournalChannel
from boba.toolkit.result import CustomElementResult
from boba.toolrun.streams import (
    JournalWatchSource,
    StreamNote,
    ToolStreams,
)
from boba.workspace.launcher import ReadWindow
from chainlit.data import get_data_layer

__all__ = [
    "AudioViewer",
    "CanvasPanel",
    "FileViewer",
    "ImageViewer",
    "LogViewer",
    "MarkdownViewer",
    "PanelStorage",
    "PdfViewer",
    "StorageHashSource",
    "StorageStatSource",
    "StorageWindows",
    "StreamActions",
    "VideoViewer",
]

logger = logging.getLogger(__name__)


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
