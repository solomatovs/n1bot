"""Tool canvas_open: показ файла workspace в панели канваса.

Вьюверы содержимого живут в canvas.panel; здесь — фасад тула, разбор
пути сессии и обработчики действий фронта (клик по ссылке, смена файла).

Ошибки: ErrorResult — нет сессии, путь вне каталогов треда, файл некому
показать, не читается или слишком велик; остальное упаковывает ToolErrorGuard.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

import chainlit as cl
from boba.canvas.canvas import (
    CanvasAction,
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasRegistry,
    OpenedCanvas,
)
from boba.canvas.keys import ObjectKey
from boba.chainlit.canvas.panel import (
    AudioViewer,
    CanvasPanel,
    ImageViewer,
    LogViewer,
    MarkdownViewer,
    PdfViewer,
    VideoViewer,
)
from boba.chainlit.domain.context import ChatCallContext
from boba.chainlit.infra.thread_room import ChatNotices
from boba.identity.errors import RefusalError
from boba.toolkit.result import ErrorResult, ToolResult, pack_result

__all__ = [
    "CanvasActions",
    "CanvasOpener",
    "CanvasPrompt",
    "CanvasScope",
    "CanvasToolConfig",
    "build_canvas_tools",
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


class CanvasScope(BaseModel):
    """Чьи файлы показывает панель: пользователь и тред.

    Тул берёт их из контекста вызова, действия фронта — из сессии чата:
    у клика по панели контекста вызова нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    thread_id: str

    @classmethod
    def of_context(cls) -> CanvasScope:
        context = ChatCallContext.require()

        return cls(user_id=context.subject.user_key, thread_id=context.scope.id)


class CanvasOpener:
    """Показ панели: единый код для тула и клика по ссылке в переписке."""

    async def open(self, path: str) -> tuple[str, ToolResult]:
        """Вызов тула: показать файл; представление для ленты отдаёт вьювер."""
        try:
            opened = await self.show(path, CanvasScope.of_context())
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        content = f"opened in the canvas: {opened.label} ({opened.path}); "
        content += CanvasPrompt.NOTE
        return content, opened.link

    async def show(self, path: str, scope: CanvasScope) -> OpenedCanvas:
        """Панель с содержимым одного файла; слежение ставит CanvasPanel."""
        key = self._key(path, scope)

        return await CanvasPanel.open(key)

    async def content(
        self, path: str, watch: bool, scope: CanvasScope
    ) -> CanvasContent:
        """Описание файла для уже открытой панели: элемент не подменяется.

        watch — слежение переезжает на новый файл вместе с показом; без него
        описание отдаётся как есть (перечитывание уже показанного файла).
        """
        key = self._key(path, scope)

        viewer = CanvasRegistry.viewer_for(key.name)
        if viewer is None:
            return CanvasPanel.notice(key)

        described = await viewer.content(key)
        if watch:
            CanvasPanel.watch_file(key, viewer, described.nonce)

        return described

    @staticmethod
    def _key(path: str, scope: CanvasScope) -> ObjectKey:
        """Ключ файла в области показа; путь вне каталогов треда — отказ."""
        try:
            return ObjectKey.from_workspace(scope.user_id, scope.thread_id, path)
        except ValueError as e:
            raise CanvasError(CanvasErrorKind.BAD_PATH, str(e)) from e


class CanvasActions:
    """Обработчики действий фронта над файлами панели.

    Действие приходит из уже открытой панели; чьи файлы показывать, говорит
    колбэк по сессии чата — контекста вызова у клика нет.
    """

    @classmethod
    async def open(cls, action: cl.Action, scope: CanvasScope) -> None:
        """Клик по файлу в списке или по ссылке в ленте: сфокусировать файл."""
        path = action.payload.get(CanvasAction.PATH.value)
        if not path:
            logger.warning("canvas action without path: %s", action.payload)
            return

        try:
            await CanvasOpener().show(str(path), scope)
        except RefusalError as e:
            await ChatNotices.error(f"Failed to open the canvas: {e}")

    @classmethod
    async def content(cls, action: cl.Action, scope: CanvasScope) -> dict[str, Any]:
        """Смена файла в открытой панели: фронт берёт описание и рисует сам.

        Панель здесь не трогается — иначе chainlit пересоздал бы её и проиграл
        анимацию открытия заново. refresh — перечитывание уже показанного
        файла по сигналу слежения: слежение не перерегистрируется.
        """
        path = action.payload.get(CanvasAction.PATH.value)
        if not path:
            logger.warning("canvas content action without path: %s", action.payload)
            return {}

        refresh = bool(action.payload.get("refresh"))

        try:
            described = await CanvasOpener().content(
                str(path), watch=not refresh, scope=scope
            )
        except RefusalError as e:
            await ChatNotices.error(f"Failed to open the canvas: {e}")
            return {}

        return described.props()


def build_canvas_tools(cfg: CanvasToolConfig) -> list[BaseTool]:
    opener = CanvasOpener()

    # вьюверы общего вида — забота самой панели; диаграммы регистрирует diagram
    CanvasRegistry.register(ImageViewer())
    CanvasRegistry.register(PdfViewer())
    CanvasRegistry.register(MarkdownViewer())
    CanvasRegistry.register(LogViewer())
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
