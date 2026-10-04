"""Панель канваса со стороны чата: вьюверы, разбор пути сессии и
обработчики действий фронта (клик по ссылке, смена файла).

Ошибки:
CanvasError — путь вне каталогов треда, файл некому показать, не
    читается или слишком велик; текст причины готов для LLM.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

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
from boba.chainlit.canvas.diagram import DiagramFiles, MermaidViewer
from boba.chainlit.canvas.panel import (
    AudioViewer,
    CanvasPanel,
    ImageViewer,
    LogViewer,
    MarkdownViewer,
    PdfViewer,
    VideoViewer,
)
from boba.chainlit.infra.thread_room import ChatNotices
from boba.identity.errors import RefusalError

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
    """Показ панели по клику: файл в панель и описание для смены файла."""

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
