"""Файловые обработчики и сохранение встроенного содержимого в файлы треда."""

import base64
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import ClassVar
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid5

from chainlit.element import Element, File, Image

from boba.aid.domain.keys import CanvasFileUrl
from boba.aid.rendering.handlers.base import (
    ContentHandler,
    HandlerContext,
    Resource,
    Target,
)
from boba.aid.rendering.tool import ChatElements
from boba.canvas.canvas import CanvasError, CanvasKind, CanvasRegistry, CanvasScope
from boba.canvas.keys import WorkspaceKey
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.storage.client import StorageClient
from boba.toolkit.result import ContentMime


class FileHandler(ContentHandler):
    """Неизвестный MIME: файл со скачиванием без попытки предпросмотра."""

    async def render(
        self, resource: Resource, target: Target, context: HandlerContext
    ) -> str:
        return await context.file(resource, target, self.preview)


class ImageHandler(FileHandler):
    preview = CanvasKind.IMAGE


class AudioHandler(FileHandler):
    preview = CanvasKind.AUDIO


class VideoHandler(FileHandler):
    preview = CanvasKind.VIDEO


class PdfHandler(FileHandler):
    preview = CanvasKind.PDF


class MermaidHandler(FileHandler):
    preview = CanvasKind.MERMAID


class ResourceFiles:
    """Хранение и ссылки. Поддержку MIME решает handler, передавая preview."""

    NAMESPACE: ClassVar[UUID] = UUID("6f9b1f4e-2f1a-4c1a-9a2f-1d3b5c7e9a11")
    BASE64_SLICE: ClassVar[int] = 64 * 1024

    def __init__(
        self, storage: StorageClient, scope: Callable[[], Awaitable[CanvasScope]]
    ) -> None:
        self._storage = storage
        self._scope = scope
        self._elements = ChatElements()

    async def render(
        self, resource: Resource, target: Target, preview: CanvasKind | None
    ) -> str:
        """ValueError — некорректный base64; файл в этом случае не записывается."""
        if resource.text is not None:
            resource = replace(resource, mime=resource.mime.utf8())
        if resource.data is not None:
            async for _ in self._decoded(resource.data):
                pass
        if not target.emits_elements:
            return ""
        try:
            if resource.uri is not None:
                element = await self._linked(resource, target, preview)
            else:
                key = await self._store(resource, target)
                element = await self._mounted(key, resource, target, preview)
        except (StorageError, CanvasError) as exc:
            return (
                f"{resource.name or resource.uri or 'Attachment'} is not shown: {exc}"
            )
        await target.attach(element, False)
        return ""

    async def _linked(
        self, resource: Resource, target: Target, preview: CanvasKind | None
    ) -> Element:
        uri = resource.uri or ""
        address = urlsplit(uri)
        if address.scheme == "file":
            scope = await self._scope()
            key = scope.key(unquote(address.path))
            return await self._mounted(key, resource, target, preview)
        if address.scheme not in {"http", "https"}:
            raise ValueError(f"unsupported resource URI scheme: {address.scheme}")
        # Вложения пользователя, загруженные AID, имеют маршрут Canvas.
        # Внешние ссылки сохраняют свой host и никогда не скачиваются сервером.
        if target.thumbnail:
            try:
                scope = await self._scope()
                key = CanvasFileUrl.key(scope, address.path)
            except (ValueError, StorageError):
                pass
            else:
                if CanvasRegistry.viewer_for(resource.mime.value) is None:
                    return File(
                        name=resource.name,
                        url=urlunsplit(("", "", address.path, address.query, "")),
                        mime=resource.mime.value,
                        display="inline",
                    )
                return await self._mounted(key, resource, target, preview)
        return File(
            name=resource.name, url=uri, mime=resource.mime.value, display="inline"
        )

    async def _mounted(
        self,
        key: WorkspaceKey,
        resource: Resource,
        target: Target,
        preview: CanvasKind | None,
    ) -> Element:
        mime = resource.mime.value
        name = resource.name or key.name
        url = CanvasFileUrl.path(key, mime)
        viewer = CanvasRegistry.viewer_for(mime) if preview is not None else None
        if viewer is not None and viewer.kind == preview:
            if target.thumbnail and preview is CanvasKind.IMAGE:
                return Image(name=name, url=url, mime=mime, display="inline")
            try:
                return self._elements.of_result(await viewer.link(key, mime))
            except CanvasError:
                if not target.thumbnail:
                    raise
        return File(name=name, url=url, mime=mime, display="inline")

    async def _store(self, resource: Resource, target: Target) -> WorkspaceKey:
        data = resource.data
        if data is None:
            # ACP text — Unicode. Сохраняем UTF-8 независимо от платформы.
            data = base64.b64encode((resource.text or "").encode("utf-8")).decode(
                "ascii"
            )
        digest = hashlib.sha256(data.encode("ascii")).hexdigest()
        mime = resource.mime.value
        suffix = ContentMime.suffix_of(resource.mime.media_type)
        if not suffix:
            suffix = PurePosixPath(resource.name).suffix
        stem = uuid5(self.NAMESPACE, f"{target.id}:{target.number}:{mime}:{digest}")
        scope = await self._scope()
        key = scope.file(f"{stem}{suffix}")
        try:
            await self._storage.stat(key.render())
        except StorageNotFoundError:
            await self._storage.upload_stream(key.render(), self._decoded(data), mime)
        return key

    async def _decoded(self, data: str) -> AsyncIterator[bytes]:
        for start in range(0, len(data), self.BASE64_SLICE):
            yield base64.b64decode(
                data[start : start + self.BASE64_SLICE], validate=True
            )
