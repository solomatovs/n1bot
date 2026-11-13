"""Единый выбор MIME → handler для сообщений, инструментов и Canvas."""

from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import mcp_types as mt
from chainlit.element import Element

from boba.aid.rendering.handlers.base import ContentHandler, Resource, Target
from boba.aid.rendering.handlers.files import (
    AudioHandler,
    FileHandler,
    ImageHandler,
    MermaidHandler,
    PdfHandler,
    ResourceFiles,
    VideoHandler,
)
from boba.aid.rendering.handlers.text import (
    CodeHandler,
    CsvHandler,
    JsonHandler,
    MarkdownHandler,
    PlainHandler,
)
from boba.aid.rendering.mime import Mime, SupportedMime
from boba.aid.rendering.tool import ChatElements
from boba.canvas.canvas import CanvasKind, CanvasScope
from boba.storage.client import StorageClient
from boba.toolkit.result import (
    BlockAudience,
    ContentMime,
    Fence,
    JsonBlock,
    VisualResult,
)


def handler_for(  # noqa: C901, PLR0911 — явный диспетчер всех поддерживаемых MIME
    mime: SupportedMime | None,
) -> ContentHandler:
    """Новый формат добавляется в SupportedMime и в эту ветку match."""
    match mime:
        case SupportedMime.MARKDOWN:
            return MarkdownHandler()
        case SupportedMime.PLAIN:
            return PlainHandler()
        case SupportedMime.CSV:
            return CsvHandler()
        case SupportedMime.JSON:
            return JsonHandler()
        case (
            SupportedMime.SQL
            | SupportedMime.SHELL
            | SupportedMime.HTML
            | SupportedMime.CSS
            | SupportedMime.JAVASCRIPT
            | SupportedMime.APPLICATION_JAVASCRIPT
            | SupportedMime.PYTHON
            | SupportedMime.XML
            | SupportedMime.TEXT_XML
            | SupportedMime.YAML
            | SupportedMime.TEXT_YAML
        ):
            return CodeHandler()
        case (
            SupportedMime.PNG
            | SupportedMime.JPEG
            | SupportedMime.GIF
            | SupportedMime.WEBP
            | SupportedMime.SVG
            | SupportedMime.AVIF
            | SupportedMime.BMP
            | SupportedMime.ICON
        ):
            return ImageHandler()
        case (
            SupportedMime.MPEG_AUDIO
            | SupportedMime.MP3
            | SupportedMime.MP4_AUDIO
            | SupportedMime.OGG_AUDIO
            | SupportedMime.WAV
            | SupportedMime.X_WAV
            | SupportedMime.FLAC
            | SupportedMime.WEBM_AUDIO
            | SupportedMime.AAC
        ):
            return AudioHandler()
        case (
            SupportedMime.MP4_VIDEO | SupportedMime.WEBM_VIDEO | SupportedMime.OGG_VIDEO
        ):
            return VideoHandler()
        case SupportedMime.PDF:
            return PdfHandler()
        case SupportedMime.MERMAID:
            return MermaidHandler()
        case _:
            return FileHandler()


class ContentRenderer:
    """Форма блока → Resource → MIME → handler. Общий путь для всей ленты."""

    def __init__(
        self, storage: StorageClient, scope: Callable[[], Awaitable[CanvasScope]]
    ) -> None:
        self._files = ResourceFiles(storage, scope)
        self._elements = ChatElements()

    async def render(self, block: mt.ContentBlock, target: Target) -> str:
        if block.annotations is not None and not BlockAudience.USER.reads(
            block.annotations.audience
        ):
            return ""
        resource = self._resource(block)
        handler = handler_for(resource.mime.supported)
        try:
            return await handler.render(resource, target, self)
        except ValueError:
            # Повреждённые данные и запрещённые URI — диагностика, не файл
            # с якобы корректными байтами. Это не fallback неизвестного MIME.
            shown = block.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude={"data": True, "resource": {"blob": True}},
            )
            return Fence.around(JsonBlock.pretty(shown), "json")

    async def file(
        self, resource: Resource, target: Target, preview: CanvasKind | None
    ) -> str:
        return await self._files.render(resource, target, preview)

    def element(self, result: VisualResult) -> Element:
        return self._elements.of_result(result)

    @staticmethod
    def _resource(block: mt.ContentBlock) -> Resource:
        match block:
            case mt.TextContent():
                return Resource(
                    mime=Mime.parse(SupportedMime.MARKDOWN), text=block.text
                )
            case mt.ImageContent() | mt.AudioContent():
                return Resource(mime=Mime.parse(block.mime_type), data=block.data)
            case mt.ResourceLink():
                return Resource(
                    mime=Mime.parse(ContentMime.named(block.mime_type, block.name)),
                    name=block.name,
                    uri=str(block.uri),
                )
            case mt.EmbeddedResource():
                content = block.resource
                name = unquote(PurePosixPath(urlsplit(str(content.uri)).path).name)
                if isinstance(content, mt.TextResourceContents):
                    return Resource(
                        mime=Mime.parse(content.mime_type or SupportedMime.PLAIN),
                        name=name,
                        text=content.text,
                    )
                return Resource(
                    mime=Mime.parse(content.mime_type or ContentMime.OCTET),
                    name=name,
                    data=content.blob,
                )
            case _:
                return Resource(
                    mime=Mime.parse(ContentMime.OCTET),
                    name="content.json",
                    text=block.model_dump_json(by_alias=True, exclude_none=True),
                )


def mimes_for(kind: CanvasKind) -> frozenset[str]:
    """Canvas получает форматы из того же диспетчера, что лента."""
    return frozenset(
        mime.value for mime in SupportedMime if handler_for(mime).preview == kind
    )
