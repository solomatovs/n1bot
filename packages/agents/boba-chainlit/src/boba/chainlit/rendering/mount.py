"""Монтирование элементов результата на поверхность чата.

Обвязка ChatMount ставится на инструменты чата после тела: проходит по
items из chat_view() результата и исполняет то, что требует поверхности —
открывает файл в панели канваса и кладёт ссылку на него в переписку
(PanelOpen), прикрепляет файл workspace вложением (FileElement). Виджеты
(VisualElement) рисует лента по концу шага. Если панель не смогла показать
файл, результат подменяется на ErrorResult: так вердикт браузера доходит
до модели тем же путём, что и любой отказ инструмента.

Ошибки:
RefusalError — вызов идёт вне хода чата или без живого запуска.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from langchain_core.tools import BaseTool

import chainlit as cl
from boba.canvas.canvas import CanvasError, CanvasErrorKind
from boba.canvas.keys import ElementProps, ObjectKey
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.domain.context import ChatCallContext
from boba.chainlit.rendering.tool import ChatElements
from boba.identity.run import ElementTarget, RunRegistry
from boba.toolkit.result import (
    ErrorResult,
    FileElement,
    PanelOpen,
    ToolResultBase,
)
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["ChatMount", "MountedCall", "WorkspaceFile"]

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceFile(cl.File):
    """Файл workspace: помимо штатных полей несёт каталог в props.

    Ссылка на вложение вычисляется при чтении треда, поэтому каталог обязан
    храниться рядом с элементом — иначе отдача ищет файл только в upload/.
    """

    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MountedCall:
    """Вызов, за которым следит обвязка: имя инструмента."""

    tool: str


class ChatMount(CallHooks[MountedCall]):
    """Обвязка чата: элементы результата, требующие поверхности."""

    RETRY_NOTE: ClassVar[str] = "fix the file and call the tool again"

    @classmethod
    def guard_all(cls, tools: Sequence[BaseTool]) -> None:
        ToolBody.hook_all(tools, cls())

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> MountedCall:
        return MountedCall(tool=name)

    async def after_async(self, ctx: MountedCall, result: object) -> object:
        """Результат тела — пара langchain (content, artifact): монтируются
        элементы артефакта; отказ поверхности подменяет пару целиком."""
        if not isinstance(result, tuple):
            return result

        artifact = result[1]
        if not isinstance(artifact, ToolResultBase):
            return result

        try:
            await self._mount(artifact)
        except CanvasError as e:
            message = f"{artifact.llm_view()}; but {e}; {self.RETRY_NOTE}"
            logger.info("tool %s: canvas refused the result: %s", ctx.tool, e)
            return ErrorResult(message=message, error_kind=e.kind).packed()

        return result

    async def _mount(self, artifact: ToolResultBase) -> None:
        for item in artifact.chat_view().items:
            if isinstance(item, PanelOpen):
                await self._panel(item)

            if isinstance(item, FileElement):
                await self._file(item)

    async def _panel(self, item: PanelOpen) -> None:
        """Панель с файлом плюс ссылка на него в переписке."""
        key = self._key(item.path)
        opened = await CanvasPanel.open(key)
        await self._publish(key.thread_id, ChatElements.of_result(opened.link))

    async def _file(self, item: FileElement) -> None:
        key = self._key(item.path)
        target = self._target(key.thread_id)
        links = AttachmentDataLayer.require().links
        element = WorkspaceFile(
            id=target.element_id,
            name=item.name,
            thread_id=key.thread_id,
            url=links.url(key.thread_id, target.element_id, key.dir),
            mime=item.mime,
            display="inline",
            props=ElementProps(dir=key.dir).model_dump(mode="json"),
        )
        await self._publish(key.thread_id, element)

    async def _publish(self, thread_id: str, element: cl.Element) -> None:
        """Строка в elements и показ через шину хода: элемент рисуют вкладки
        треда на всех инстансах и он переживает перезагрузку треда."""
        target = self._target(thread_id)
        element.id = target.element_id
        element.thread_id = thread_id
        element.for_id = target.for_id
        await AttachmentDataLayer.require().create_element(element)

        context = ChatCallContext.require()
        port = RunRegistry.require_port(thread_id)
        await port.show_element(context.tool_call_id(), element.to_dict())

    @staticmethod
    def _key(path: str) -> ObjectKey:
        """Ключ файла в области хода; путь вне каталогов треда — отказ."""
        context = ChatCallContext.require()
        try:
            return ObjectKey.from_workspace(
                context.subject.user_key, context.scope.id, path
            )
        except ValueError as e:
            raise CanvasError(CanvasErrorKind.BAD_PATH, str(e)) from e

    @staticmethod
    def _target(thread_id: str) -> ElementTarget:
        context = ChatCallContext.require()
        port = RunRegistry.require_port(thread_id)

        return port.element_target(context.tool_call_id())
