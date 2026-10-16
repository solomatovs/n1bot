"""Панель канваса со стороны чата: вьюверы, разбор пути сессии и
обработчики действий фронта (клик по ссылке, смена файла).

Ошибки:
CanvasError — путь вне каталогов треда, файл некому показать, не
    читается или слишком велик; текст причины готов для LLM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote, urlsplit

import chainlit as cl
import mcp_types as mt
from chainlit.context import context_var
from pydantic import BaseModel, ConfigDict

from boba.aid.canvas.diagram import DiagramFiles, MermaidViewer
from boba.aid.canvas.panel import (
    AudioViewer,
    CanvasPanel,
    ImageViewer,
    LogViewer,
    MarkdownViewer,
    PdfViewer,
    VideoViewer,
)
from boba.aid.infra.thread_room import ChatNotices, ChatRoomSurface, ThreadRoom
from boba.aid.rendering.chat_view import ChatView
from boba.canvas.canvas import (
    CanvasAction,
    CanvasContent,
    CanvasError,
    CanvasErrorKind,
    CanvasRegistry,
    OpenedCanvas,
)
from boba.canvas.keys import ObjectKey
from boba.identity.errors import RefusalError
from boba.toolkit.result import BlockAudience, ContentMime
from boba.toolkit.wire import McpContent

__all__ = [
    "CanvasActions",
    "CanvasOpener",
    "CanvasScope",
    "CanvasViewers",
]

logger = logging.getLogger(__name__)


class CanvasViewers:
    """Вьюверы панели: регистрируются процессом чата на старте, чтобы панель
    открывалась кликом до первого хода."""

    @staticmethod
    def register_all() -> None:
        CanvasRegistry.register(ImageViewer())
        CanvasRegistry.register(PdfViewer())
        CanvasRegistry.register(MarkdownViewer())
        CanvasRegistry.register(LogViewer())
        CanvasRegistry.register(VideoViewer())
        CanvasRegistry.register(AudioViewer())
        CanvasRegistry.register(MermaidViewer(DiagramFiles()))


class CanvasScope(BaseModel):
    """Чьи файлы показывает панель: пользователь и тред из сессии чата —
    у клика по панели контекста вызова нет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    thread_id: str


class CanvasOpener:
    """Показ панели: по клику — файл в панель и описание для смены файла; по
    ответу tools/call — файлы, которые сервер просит показать сразу.

    Ответ показывает ретранслятор chainlit, прежде чем отдать его агенту:
    панель открывается во вкладках треда этого инстанса, у диаграммы
    вьювер ждёт вердикт браузера, и отказ показа модель видит в том же
    вызове.
    """

    async def show(self, path: str, scope: CanvasScope) -> OpenedCanvas:
        """Панель с содержимым одного файла; слежение ставит CanvasPanel."""
        key = self._key(path, scope)

        return await CanvasPanel.open(key, ContentMime.of_name(key.name))

    async def content(
        self, path: str, watch: bool, scope: CanvasScope
    ) -> CanvasContent:
        """Описание файла для уже открытой панели: элемент не подменяется.

        watch — слежение переезжает на новый файл вместе с показом; без него
        описание отдаётся как есть (перечитывание уже показанного файла).
        """
        key = self._key(path, scope)

        viewer = CanvasRegistry.viewer_for(ContentMime.of_name(key.name))
        if viewer is None:
            return CanvasPanel.notice(key)

        described = await viewer.content(key)
        if watch:
            CanvasPanel.watch_file(key, viewer, described.nonce)

        return described

    async def reply(
        self, scope: CanvasScope, reply: mt.CallToolResult
    ) -> mt.CallToolResult:
        """Ответ tools/call после показа его файлов в панели треда scope.

        Ссылка file:// человеку с annotations.priority McpContent.SHOWN_PRIORITY
        открывает панель; отказ показа (диаграмма не отрисовалась, вьювера
        нет, файл не читается) превращает ответ в isError с причиной. Без
        живых вкладок треда на этом инстансе и при молчании браузера ответ
        уходит как есть.
        """
        sessions = ThreadRoom.sessions(scope.thread_id)
        if not sessions:
            return reply

        context = ChatRoomSurface(sessions[0], scope.thread_id).context()
        if context is None:
            return reply

        refusals: list[mt.ContentBlock] = []
        token = context_var.set(context)
        try:
            for path, mime in self._shown(reply):
                try:
                    await CanvasPanel.open(self._key(path, scope), mime)
                except CanvasError as exc:
                    note = f"{path} is not shown in the canvas: {exc}"
                    refusals.append(mt.TextContent(text=note))
        finally:
            context_var.reset(token)

        if not refusals:
            return reply

        content = [*reply.content, *refusals]

        return reply.model_copy(update={"content": content, "is_error": True})

    @staticmethod
    def _shown(reply: mt.CallToolResult) -> Iterator[tuple[str, str]]:
        """Путь в workspace и MIME файлов ответа, которые сервер просит
        показать человеку сразу."""
        for block in reply.content:
            if not isinstance(block, mt.ResourceLink):
                continue

            annotations = block.annotations
            if annotations is None:
                continue

            if not BlockAudience.USER.reads(annotations.audience):
                continue

            if annotations.priority != McpContent.SHOWN_PRIORITY:
                continue

            address = urlsplit(str(block.uri))
            if address.scheme != ChatView.FILE_SCHEME:
                continue

            path = unquote(address.path)

            mime = block.mime_type
            if mime is None:
                mime = ContentMime.of_name(path)

            yield path, mime

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
