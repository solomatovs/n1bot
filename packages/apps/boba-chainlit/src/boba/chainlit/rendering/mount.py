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

import asyncio
import logging
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import uuid4

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool

import chainlit as cl
from boba.canvas.canvas import CanvasError, CanvasErrorKind
from boba.canvas.keys import ElementProps, ObjectKey
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.data.data_layer import AttachmentDataLayer, HeldContent
from boba.chainlit.domain.context import ChatCallContext
from boba.chainlit.rendering.tool import ChatElements
from boba.identity.context import CallContexts, ContextKind
from boba.identity.errors import RefusalError
from boba.identity.run import ElementTarget, Runs
from boba.mcp_client.client import BlockFiles
from boba.runtime.storage import StorageClient
from boba.toolkit.result import (
    ErrorResult,
    FileElement,
    PanelOpen,
    ToolResultBase,
)
from boba.toolrun.stream_calls import CallReply, ToolServer
from boba.toolrun.wrapping import CallHooks, ToolBody

__all__ = ["ChatAttachments", "ChatMount", "MountedCall", "WorkspaceFile"]

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

    def __init__(self, contexts: CallContexts, runs: Runs) -> None:
        self._contexts = contexts
        self._runs = runs

    def guard_all(self, tools: Sequence[BaseTool]) -> None:
        ToolBody.hook_all(tools, self)

    def chat_context(self) -> ChatCallContext:
        """Контекст хода чата; вызов вне чата — RefusalError(CHAT_ONLY)."""
        context = self._contexts.current()
        if isinstance(context, ChatCallContext):
            return context

        got = type(context).__name__
        msg = f"this tool works only inside a chat turn, called from {got}"
        raise RefusalError(ContextKind.CHAT_ONLY, msg)

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

    async def attach(self, item: FileElement) -> None:
        """Прикрепляет файл workspace вложением к шагу текущего вызова."""
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

        context = self.chat_context()
        port = self._runs.require_port(thread_id)
        await port.show_element(context.tool_call_id(), element.to_dict())

    def _key(self, path: str) -> ObjectKey:
        """Ключ файла в области хода; путь вне каталогов треда — отказ."""
        context = self.chat_context()
        try:
            return ObjectKey.from_workspace(
                context.subject.user_key, context.scope.id, path
            )
        except ValueError as e:
            raise CanvasError(CanvasErrorKind.BAD_PATH, str(e)) from e

    def _target(self, thread_id: str) -> ElementTarget:
        context = self.chat_context()
        port = self._runs.require_port(thread_id)

        return port.element_target(context.tool_call_id())


class MountedToolServer(ToolServer):
    """Порт ToolServer, монтирующий результат удалённого вызова на поверхность
    чата.

    Создаётся сборкой агента поверх порта MCP-сервера. Тело инструмента
    исполнил сервер и о чате не знает; когда вызов вернулся, порт отдаёт его
    результат обвязке ChatMount — та открывает файл в панели, прикрепляет
    вложение. Отказ поверхности (диаграмма не отрисовалась) подменяет итог
    вызова ошибкой, и модель видит его как отказ инструмента.
    """

    def __init__(
        self, inner: ToolServer, mount: ChatMount, contexts: CallContexts
    ) -> None:
        self._inner = inner
        self._mount = mount
        self._contexts = contexts

    def tools(self) -> Sequence[BaseTool]:
        return self._inner.tools()

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._inner.features()

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        accepted = await self._inner.submit(calls)
        mounted: list[asyncio.Future[ToolMessage]] = []
        for call, future in zip(calls, accepted, strict=True):
            mounted.append(asyncio.ensure_future(self._mounted(call, future)))

        return mounted

    async def _mounted(
        self, call: ToolCall, future: asyncio.Future[ToolMessage]
    ) -> ToolMessage:
        message = await future
        artifact = message.artifact
        if not isinstance(artifact, ToolResultBase):
            return message

        call_id = str(call["id"])
        pair = (message.content, artifact)
        context = self._contexts.current()
        with self._contexts.applied(context.as_tool_call(call_id)):
            result = await self._mount.after_async(MountedCall(tool=call["name"]), pair)

        if result is pair:
            return message

        if not isinstance(result, tuple):
            return message

        refusal = result[1]
        if not isinstance(refusal, ToolResultBase):
            return message

        return CallReply(call).message(refusal, True)


class ChatAttachments(BlockFiles):
    """Реализация BlockFiles чатом: файл из результата MCP-сервера ложится в
    workspace треда и показывается вложением.

    Создаётся сборкой чата из держателя контекста, хранилища вложений и
    обвязки ChatMount. Файл пишется в каталог upload треда того хода, в
    котором идёт вызов, поэтому виден и пользователю, и инструментам
    песочницы. Размер ограничен самим workspace пользователя. Вложением
    показывается первый файл вызова: элемент вызова у шага один; остальные
    лежат в workspace, и путь каждого назван в тексте результата.
    """

    def __init__(
        self, contexts: CallContexts, storage: StorageClient, mount: ChatMount
    ) -> None:
        self._contexts = contexts
        self._storage = storage
        self._mount = mount

    async def attached(self, call: ToolCall, index: int, mime: str, data: bytes) -> str:
        call_id = str(call["id"])
        context = self._contexts.current()
        key = ObjectKey.build(
            context.subject.user_key,
            context.scope.id,
            self._name(call, index, mime),
            call_id,
        )
        await self._storage.upload_stream(
            key.render(), HeldContent(data).chunks(), mime
        )

        path = key.in_workspace()
        if index == 0:
            item = FileElement(path=path, name=key.name, mime=mime)
            with self._contexts.applied(context.as_tool_call(call_id)):
                await self._mount.attach(item)

        return f"file saved to the workspace: {path} ({mime}, {len(data)} bytes)"

    @staticmethod
    def _name(call: ToolCall, index: int, mime: str) -> str:
        extension = mimetypes.guess_extension(mime)
        if extension is None:
            extension = ""

        return f"{call['name']}-{uuid4().hex[:8]}-{index}{extension}"
