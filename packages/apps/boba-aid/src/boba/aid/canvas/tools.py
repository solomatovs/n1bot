"""Панель канваса со стороны чата: вьюверы и показ файлов — по клику и
по ответу tools/call.

Ошибки:
CanvasError — путь вне каталогов треда, файл некому показать, не
    читается или слишком велик; текст причины готов для LLM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import mcp_types as mt
from chainlit.context import context_var

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
from boba.aid.rendering.chat_view import WorkspaceFile, WorkspaceLinks
from boba.aid.rendering.renderer import TabSurfaces
from boba.canvas.canvas import (
    CanvasContent,
    CanvasError,
    CanvasRegistry,
    CanvasScope,
    OpenedCanvas,
)
from boba.toolkit.result import BlockAudience
from boba.toolkit.wire import McpContent

__all__ = [
    "CanvasOpener",
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


class CanvasOpener:
    """Показ панели: по клику — файл в панель и описание для смены файла; по
    ответу tools/call — файлы, которые сервер просит показать сразу.

    Ответ показывает ретранслятор чата aid, прежде чем отдать его агенту:
    панель открывается во вкладках треда этого инстанса, у диаграммы
    вьювер ждёт вердикт браузера, и отказ показа модель видит в том же
    вызове. Создаётся провайдером canvas_opener; поверхность живой вкладки
    треда даёт TabSurfaces.
    """

    def __init__(self, surfaces: TabSurfaces) -> None:
        self._surfaces = surfaces
        self._links = WorkspaceLinks()

    async def show(self, path: str, mime: str, scope: CanvasScope) -> OpenedCanvas:
        """Панель с содержимым одного файла вида mime — того, каким его
        назвала ссылка. Слежение ставит CanvasPanel."""
        key = scope.key(path)

        return await CanvasPanel.open(key, mime)

    async def content(
        self, path: str, mime: str, watch: bool, scope: CanvasScope
    ) -> CanvasContent:
        """Описание файла вида mime для уже открытой панели: элемент не
        подменяется.

        watch — слежение переезжает на новый файл вместе с показом; без него
        описание отдаётся как есть (перечитывание уже показанного файла).
        """
        key = scope.key(path)

        viewer = CanvasRegistry.viewer_for(mime)
        if viewer is None:
            return CanvasPanel.notice(key, mime)

        if not watch:
            return await viewer.content(key, mime)

        file_watch = await CanvasPanel.watch_file(key, viewer)
        described = await viewer.content(key, mime)
        file_watch.start(described.nonce)

        return file_watch.described(described)

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
        shown = list(self._shown(reply))
        if not shown:
            return reply

        context = self._surfaces.live(scope.scope_id).context()
        if context is None:
            return reply

        refusals: list[mt.ContentBlock] = []
        token = context_var.set(context)
        try:
            for mounted in shown:
                try:
                    await CanvasPanel.open(scope.key(mounted.path), mounted.mime)
                except CanvasError as exc:
                    note = f"{mounted.path} is not shown in the canvas: {exc}"
                    refusals.append(mt.TextContent(text=note))
        finally:
            context_var.reset(token)

        if not refusals:
            return reply

        content = [*reply.content, *refusals]

        return reply.model_copy(update={"content": content, "is_error": True})

    def _shown(self, reply: mt.CallToolResult) -> Iterator[WorkspaceFile]:
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

            if mounted := self._links.file_of(block):
                yield mounted
