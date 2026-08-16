"""Tool canvas_open и базовые вьюверы содержимого панели.

Панель показывает один файл — тот, что открыла LLM тулом или ткнул
пользователь по ссылке в переписке; рисует его вьювер из CanvasRegistry.
Здесь живут вьюверы общего вида — картинки, pdf, текст (их генерируют
bash/python-тулы); диаграммы регистрирует плагин diagram.

Ошибки: ErrorResult — нет сессии, путь вне каталогов треда, файл некому
показать, не читается или слишком велик; остальное упаковывает ToolErrorGuard.
"""

from __future__ import annotations

import logging
import mimetypes
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

import chainlit as cl
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import StorageClient
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.keys import CanvasFileUrl, ObjectKey
from boba.chainlit.domain.session import RequiredSession
from boba.chainlit.rendering.canvas import (
    CanvasAction,
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasKind,
    CanvasPanel,
    CanvasPush,
    CanvasRegistry,
    OpenedCanvas,
)
from boba.chainlit.rendering.errors import show_error
from boba.toolkit.result import (
    CustomElementResult,
    ErrorResult,
    ToolResult,
    pack_result,
)
from chainlit.data import get_data_layer

__all__ = [
    "AudioViewer",
    "CanvasOpener",
    "CanvasPrompt",
    "CanvasToolConfig",
    "FileViewer",
    "ImageViewer",
    "PdfViewer",
    "TextViewer",
    "VideoViewer",
    "build_canvas_tools",
    "canvas_content_action",
    "open_canvas_action",
]

logger = logging.getLogger(__name__)


class CanvasToolConfig(BaseModel):
    """Секция [tool.canvas]: у панели своих параметров нет."""

    model_config = ConfigDict(extra="ignore")


class CanvasPrompt(StrEnum):
    """Тексты фасада: описание параметра и оговорка для LLM."""

    PATH = (
        "Путь к файлу в workspace треда: '/workspace/<thread_id>/mermaid/<имя>.mmd' "
        "или '/workspace/<thread_id>/upload/<имя>'. Файл должен существовать. "
        "Поддерживаются диаграммы mermaid (.mmd), изображения "
        "(.png/.jpg/.jpeg/.gif/.svg/.webp), .pdf, текст (.txt/.md/.log), "
        "видео (.mp4/.webm/.mov) и аудио (.mp3/.wav/.ogg/.m4a/.flac) — "
        "сгенерируй файл любым инструментом и покажи его здесь."
    )
    NOTE = (
        "the panel is open for the user and a link to it stays in the chat; "
        "mermaid files are rendered by the browser and a render failure "
        "comes back to you as a tool error"
    )


class CanvasOpener:
    """Показ панели: единый код для тула и клика по ссылке в переписке."""

    async def open(self, path: str) -> tuple[str, ToolResult]:
        """Вызов тула: показать файл; представление для ленты отдаёт вьювер."""
        try:
            opened = await self.show(path)
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        content = f"opened in the canvas: {opened.label} ({opened.path}); "
        content += CanvasPrompt.NOTE
        return content, opened.link

    async def show(self, path: str) -> OpenedCanvas:
        """Панель с содержимым одного файла."""
        user_id, thread_id = self._session()
        key = self._key(user_id, thread_id, path)

        return await CanvasPanel.open(key)

    async def content(self, path: str) -> CanvasContent:
        """Описание файла для уже открытой панели: элемент не подменяется."""
        user_id, thread_id = self._session()
        key = self._key(user_id, thread_id, path)

        viewer = CanvasRegistry.viewer_for(key.name)
        if viewer is None:
            return CanvasPanel.notice(key)

        return await viewer.content(key)

    @staticmethod
    def _session() -> tuple[str, str]:
        session = RequiredSession.of()
        return session.user_id, session.thread_id

    @staticmethod
    def _key(user_id: str, thread_id: str, path: str) -> ObjectKey:
        try:
            return ObjectKey.from_workspace(user_id, thread_id, path)
        except ValueError as e:
            raise CanvasError(CanvasErrorKind.BAD_PATH, str(e)) from e

    @staticmethod
    def _storage() -> StorageClient:
        layer = get_data_layer()
        if not isinstance(layer, AttachmentDataLayer):
            msg = f"data layer does not address attachments: {type(layer)}"
            raise RuntimeError(msg)
        return layer.storage


class FileViewer:
    """База вьюверов: файл описывается ссылкой на storage, а не телом в памяти.

    Показ статичен: перегенерированный файл открывается повторным canvas_open.
    """

    LINK_ELEMENT: ClassVar[str] = "CanvasLink"
    """Имя jsx-компонента ссылки: public/elements/CanvasLink.jsx."""

    kind: ClassVar[CanvasKind] = CanvasKind.NOTICE
    suffixes: ClassVar[frozenset[str]] = frozenset()

    def handles(self, name: str) -> bool:
        lowered = name.lower()

        return any(lowered.endswith(suffix) for suffix in self.suffixes)

    async def content(self, key: ObjectKey) -> CanvasContent:
        return CanvasContent(
            kind=self.kind,
            path=key.in_workspace(),
            label=key.name,
            url=self._serve(key),
            mime=self._mime(key.name),
        )

    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        await push(await self.content(key))

        link = CustomElementResult(
            element=self.LINK_ELEMENT,
            props={"path": key.in_workspace(), "label": key.name},
            title=key.name,
        )
        return OpenedCanvas(label=key.name, path=key.in_workspace(), link=link)

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


class TextViewer(FileViewer):
    """Текстовые файлы: markdown рендерится, остальное показывается как есть."""

    kind: ClassVar[CanvasKind] = CanvasKind.TEXT
    suffixes: ClassVar[frozenset[str]] = frozenset({".txt", ".md", ".log"})


async def open_canvas_action(action: cl.Action) -> None:
    """Клик по файлу в списке или по ссылке в ленте: сфокусировать файл."""
    path = action.payload.get(CanvasAction.PATH.value)
    if not path:
        logger.warning("canvas action without path: %s", action.payload)
        return

    try:
        await CanvasOpener().show(str(path))
    except RefusalError as e:
        await show_error(f"Failed to open the canvas: {e}")


async def canvas_content_action(action: cl.Action) -> dict[str, Any]:
    """Смена файла в открытой панели: фронт берёт описание и рисует сам.

    Панель здесь не трогается — иначе chainlit пересоздал бы её и проиграл
    анимацию открытия заново.
    """
    path = action.payload.get(CanvasAction.PATH.value)
    if not path:
        logger.warning("canvas content action without path: %s", action.payload)
        return {}

    try:
        content = await CanvasOpener().content(str(path))
    except RefusalError as e:
        await show_error(f"Failed to open the canvas: {e}")
        return {}

    return content.props()


def build_canvas_tools(cfg: CanvasToolConfig) -> list[BaseTool]:
    opener = CanvasOpener()

    # вьюверы общего вида — забота самой панели; диаграммы регистрирует diagram
    CanvasRegistry.register(ImageViewer())
    CanvasRegistry.register(PdfViewer())
    CanvasRegistry.register(TextViewer())
    CanvasRegistry.register(VideoViewer())
    CanvasRegistry.register(AudioViewer())

    @tool(response_format="content_and_artifact")
    async def canvas_open(
        path: Annotated[
            str,
            Field(min_length=1, description=CanvasPrompt.PATH),
        ],
    ) -> tuple[str, ToolResult]:
        """Показать файл workspace (диаграмму, изображение, pdf, текст) в
        панели справа от чата и оставить ссылку на него в переписке."""
        return await opener.open(path)

    return [canvas_open]
