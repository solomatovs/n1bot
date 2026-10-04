"""Вьювер .mmd для канваса: спека mermaid из workspace, показ — панелью,
вердикт рендера — от браузера; карточка диаграммы для переписки.

Ошибки:
CanvasError — файл не найден, не отдан хранилищем, не текст, слишком
    велик или не отрисовался в браузере; текст причины готов для LLM.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, ClassVar

from boba.canvas.canvas import (
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPush,
    OpenedCanvas,
    RenderStatus,
    RenderVerdicts,
    WatchSource,
)
from boba.canvas.diagram import DiagramEntry, DiagramMarker
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.chainlit.canvas.panel import FileViewer, StorageHashSource
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.toolkit.result import ChatElement, VisualResult
from boba.workspace.launcher import ReadWindow

__all__ = [
    "DiagramCard",
    "DiagramFiles",
    "MermaidViewer",
]

logger = logging.getLogger(__name__)


class DiagramFiles:
    """Спеки mermaid в каталоге mermaid/ треда: чтение из хранилища."""

    MAX_BYTES: ClassVar[int] = 1 << 20
    """Потолок файла для панели: спека больше не показывается."""

    def __init__(self, max_bytes: int = MAX_BYTES) -> None:
        self._max_bytes = max_bytes

    async def read(self, key: ObjectKey) -> str:
        """Спека целиком в памяти: её размер ограничен, а хранилище лишь стримит."""
        try:
            blob = await self._collect(key)
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

        try:
            return blob.decode(DiagramMarker.ENCODING)
        except UnicodeDecodeError as e:
            msg = f"the file is not utf-8 text: {key.in_workspace()}: {e}"
            raise CanvasError(CanvasErrorKind.BAD_FILE, msg) from e

    async def _collect(self, key: ObjectKey) -> bytes:
        """Читает файл потоком; слишком большой отвергается по размеру, до тела."""
        storage = AttachmentDataLayer.require().storage

        async with await storage.open_stream(key.render(), ReadWindow.entire()) as body:
            if body.stat.size > self._max_bytes:
                msg = (
                    f"the file is larger than the diagram limit of {self._max_bytes} "
                    f"bytes: {key.in_workspace()} has {body.stat.size} bytes"
                )
                raise CanvasError(CanvasErrorKind.TOO_LARGE, msg)

            collected = bytearray()
            async for chunk in body.chunks:
                collected.extend(chunk)

        return bytes(collected)


class MermaidViewer(FileViewer):
    """Вьювер канваса для .mmd: описывает диаграмму и ждёт вердикт рендера.

    Наследует базу вьюверов: путь, подпись и ссылку на файл проставляет она,
    поэтому спеку можно скачать так же, как любой другой показанный файл.

    Синтаксис спеки знает только mermaid.js в браузере, поэтому после показа
    вьювер ждёт canvas_render_status по nonce: FAILED — CanvasError с текстом
    ошибки mermaid, молчание браузера показу не мешает.
    """

    kind: ClassVar[CanvasKind] = CanvasKind.MERMAID
    suffixes: ClassVar[frozenset[str]] = frozenset({DiagramMarker.SUFFIX})

    VERDICT_TIMEOUT_SEC: ClassVar[float] = 10.0

    def __init__(self, files: DiagramFiles) -> None:
        self._files = files

    async def content(self, key: ObjectKey) -> CanvasContent:
        return self._content(key, await self._read(key), str(uuid.uuid4()))

    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        text = await self._read(key)
        nonce = str(uuid.uuid4())

        RenderVerdicts.expect(nonce)
        await push(self._content(key, text, nonce))

        verdict = await RenderVerdicts.wait(nonce, self.VERDICT_TIMEOUT_SEC)
        if verdict.status is RenderStatus.FAILED:
            raise CanvasError(
                CanvasErrorKind.RENDER_FAILED,
                f"the diagram does not render in the browser: {verdict.message}",
            )

        entry = DiagramEntry.of(key, text)
        link = DiagramCard.link(entry)

        return OpenedCanvas(label=entry.label, path=entry.path, nonce=nonce, link=link)

    def watch_source(self, key: ObjectKey) -> WatchSource | None:
        """Слежение по содержимому: спека мала, а размер может не меняться."""

        async def read() -> str:
            return await self._read(key)

        return StorageHashSource(read)

    async def _read(self, key: ObjectKey) -> str:
        return await self._files.read(key)

    def _content(self, key: ObjectKey, text: str, nonce: str) -> CanvasContent:
        """Спека поверх описания базы: подпись берётся из заголовка диаграммы."""
        entry = DiagramEntry.of(key, text)

        described = self.describe(key, text=entry.spec)
        return described.model_copy(update={"label": entry.label, "nonce": nonce})


class DiagramCard:
    """Карточка диаграммы в ленте: кликабельная ссылка на файл у шага ответа.

    Вердикт рендера с карточки не собирается: сообщение шага ответа появляется
    в DOM только с финальным ответом хода — во время инструмента карточке не на
    чем смонтироваться. Верификацию спеки делает показ в панели (diagram_save),
    поэтому карточка уходит без nonce и браузер по ней не отчитывается.
    """

    @classmethod
    def link(cls, entry: DiagramEntry) -> VisualResult:
        """Карточка как результат инструмента: ссылка на файл в ленте."""
        return VisualResult(
            element=ChatElement.CANVAS_VIEW,
            props=cls.props_of(entry),
            title=entry.label,
        )

    @staticmethod
    def props_of(entry: DiagramEntry) -> dict[str, Any]:
        """Props компактной карточки CanvasView: содержимое панели плюс preview."""
        content = CanvasContent(
            kind=CanvasKind.MERMAID,
            path=entry.path,
            label=entry.label,
            text=entry.spec,
        )

        return {**content.props(), "preview": True}
