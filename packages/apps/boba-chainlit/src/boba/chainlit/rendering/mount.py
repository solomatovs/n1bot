"""Вызов инструмента на поверхности чата: шаг ленты хода и монтирование
элементов результата.

ChatCalls — путь вызова узла чата (NodeCalls): сообщает владельцу идущего
хода начало вызова и его итог — по ним лента рисует шаг.
ChatMount получает от него итог вызова, когда
тело ответило, а шаг вызова ещё открыт: проходит по items из chat_view()
результата и исполняет то, что требует поверхности —
открывает файл в панели канваса и кладёт ссылку на него в переписку
(PanelOpen), прикрепляет файл workspace вложением (FileElement). Виджеты
(VisualElement) рисует лента по концу шага. Если панель не смогла показать
файл, итог вызова подменяется отказом: так вердикт браузера доходит до
модели тем же путём, что и любой отказ инструмента.

Ошибки:
RefusalError — вызов идёт вне хода чата или без живого запуска.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import uuid4

import chainlit as cl
from boba.canvas.canvas import CanvasError, CanvasErrorKind
from boba.canvas.keys import ElementProps, ObjectKey
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.data.data_layer import AttachmentDataLayer, HeldContent
from boba.chainlit.domain.context import ChatCallContext
from boba.chainlit.rendering.tool import ChatElements
from boba.connection_broker.sealing import SentConnections
from boba.identity.context import CallContexts, ContextKind
from boba.identity.errors import RefusalError
from boba.identity.run import ElementTarget, RunPort, Runs
from boba.mcp_client.client import (
    BlockFiles,
    CallSignals,
    DroppedSignals,
    ProgressListener,
)
from boba.runtime.storage import StorageClient
from boba.toolkit.calls import CallViews
from boba.toolkit.dag import (
    DagNode,
    NodeBody,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
)
from boba.toolkit.result import (
    ErrorResult,
    FileElement,
    PanelOpen,
    ToolResultBase,
)

__all__ = ["ChatAttachments", "ChatCalls", "ChatMount", "WorkspaceFile"]

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceFile(cl.File):
    """Файл workspace: помимо штатных полей несёт каталог в props.

    Ссылка на вложение вычисляется при чтении треда, поэтому каталог обязан
    храниться рядом с элементом — иначе отдача ищет файл только в upload/.
    """

    props: dict[str, Any] = field(default_factory=dict)


class ChatMount:
    """Монтирование результата вызова на поверхность чата.

    Тело инструмента о чате не знает: исполняет его свой исполнитель либо
    MCP-сервер. Когда тело ответило, путь вызова узла (ChatCalls) отдаёт итог
    сюда, пока шаг вызова ещё открыт: панель канваса открывается, вложение
    прикрепляется к шагу ответа. Отказ поверхности (диаграмма не
    отрисовалась) подменяет итог вызова отказом, и модель видит его как
    отказ инструмента. Создаётся сборкой чата из держателя контекста и
    реестра запусков: владельца хода находит через Runs.
    """

    RETRY_NOTE: ClassVar[str] = "fix the file and call the tool again"

    def __init__(self, contexts: CallContexts, runs: Runs) -> None:
        self._contexts = contexts
        self._runs = runs
        self._outcomes = NodeOutcomes()

    def chat_context(self) -> ChatCallContext:
        """Контекст хода чата; вызов вне чата — RefusalError(CHAT_ONLY)."""
        context = self._contexts.current()
        if isinstance(context, ChatCallContext):
            return context

        got = type(context).__name__
        msg = f"this tool works only inside a chat turn, called from {got}"
        raise RefusalError(ContextKind.CHAT_ONLY, msg)

    async def mounted(self, call: DagNode, outcome: NodeOutcome) -> NodeOutcome:
        """Итог вызова call после монтирования элементов его результата;
        отказ поверхности подменяет итог отказом вызова.

        Ошибки:
        RefusalError — вызов идёт вне хода чата или без живого запуска.
        """
        artifact = outcome.artifact
        try:
            await self._mount(artifact)
        except CanvasError as e:
            message = f"{artifact.llm_view()}; but {e}; {self.RETRY_NOTE}"
            logger.info("tool %s: canvas refused the result: %s", call.tool, e)
            refusal = ErrorResult(message=message, error_kind=e.kind)

            return self._outcomes.refused(call, refusal)

        return outcome

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


class ChatCalls(NodeCalls):
    """Реализация NodeCalls чатом: вызов узла виден в ленте хода шагом.

    Создаётся сборкой чата один на процесс и отдаётся исполнителям узлов:
    реестру своих инструментов (DagRunner) и портам MCP-серверов.
    Исполнитель зовёт conducted() на каждый узел — вызов модели, узел
    связки, вызов подготовки. Владельца идущего хода находит по контексту
    вызова в реестре запусков (Runs) и сообщает ему начало вызова, ждёт тело
    узла, монтирует результат на поверхность чата (ChatMount) и сообщает
    итог: результат тела либо отказ вызова. Аргументы шаг получает такими,
    какими их дала модель: запечатанное соединение заменяется ссылкой
    (SentConnections). Запуск без владельца с лентой (вызов вне хода)
    исполняет тело напрямую. Оборванный вызов шаг не закрывает: его закроет
    исход хода. Сборка сессии называет карточки инструментов MCP-серверов
    (shown): вход шага их вызова лента рисует по виду аргументов карточки.
    """

    def __init__(
        self,
        contexts: CallContexts,
        runs: Runs,
        sent: SentConnections,
        mount: ChatMount,
        journals: RemoteJournals,
    ) -> None:
        self._contexts = contexts
        self._runs = runs
        self._sent = sent
        self._mount = mount
        self._journals = journals
        self._views = CallViews()
        self._served: set[str] = set()

    def shown(self, cards: Iterable[ToolCard]) -> None:
        """Инструменты серверов cards: вход шага их вызова лента рисует по
        типам содержимого аргументов из схемы карточки, а не моделью вызова
        своего инструмента."""
        for card in cards:
            self._served.add(card.name)

    def _media_of(self, card: ToolCard) -> Mapping[str, str] | None:
        """Типы содержимого аргументов инструмента сервера; None —
        инструмент свой."""
        if card.name not in self._served:
            return None

        return self._views.media_of(card.parameters)

    async def conducted(
        self, card: ToolCard, node: DagNode, body: NodeBody
    ) -> NodeOutcome:
        context = self._contexts.current()
        port = self._runs.port_of(context.run_id)
        if port is None:
            return await body(node)

        started = time.monotonic()
        logger.info("tool %s started: call=%s", card.name, node.key)
        await port.tool_started(
            node.key, card.name, self._sent.shown(node.args), self._media_of(card)
        )

        outcome = await body(node)
        if address := outcome.journal:
            self._journals.remember(context.scope.id, node.key, address)

        with self._contexts.applied(context.as_tool_call(node.key)):
            outcome = await self._mount.mounted(node, outcome)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if outcome.errored:
            logger.info(
                "tool %s failed: call=%s in %dms: %s",
                card.name,
                node.key,
                elapsed_ms,
                outcome.content,
            )
            await port.tool_failed(node.key, outcome.artifact.chat_view().markdown)

            return outcome

        logger.info(
            "tool %s finished: call=%s output=%d chars in %dms",
            card.name,
            node.key,
            len(outcome.content),
            elapsed_ms,
        )
        await port.tool_finished(node.key, outcome.artifact)

        return outcome


class StepProgress(ProgressListener):
    """Отчёты сервера о ходе работы одного вызова — шагу этого вызова в
    ленте. Создаётся ChatProgress.listener() в контексте вызова; отчёты
    приходят позже, из задачи сессии MCP."""

    def __init__(self, port: RunPort, call_id: str) -> None:
        self._port = port
        self._call_id = call_id

    async def progressed(self, done: float, total: float | None, text: str) -> None:
        await self._port.tool_progressed(self._call_id, done, total, text)


class ChatProgress(CallSignals):
    """Реализация CallSignals клиента MCP в чате: отчёты серверов о ходе
    работы вызовов уходят владельцу запуска (RunPort), и лента показывает
    их в шаге вызова. Объект один на процесс: его создаёт сборка чата и
    отдаёт клиенту MCP. Вызов вне хода с лентой отчётов не показывает.
    """

    def __init__(self, contexts: CallContexts, runs: Runs) -> None:
        self._contexts = contexts
        self._runs = runs
        self._dropped = DroppedSignals()

    def listener(self, server: str, call: DagNode) -> ProgressListener:
        context = self._contexts.current()
        port = self._runs.port_of(context.run_id)
        if port is None:
            return self._dropped

        return StepProgress(port, call.key)


class ChatAttachments(BlockFiles):
    """Реализация BlockFiles чатом: файл из результата MCP-сервера ложится в
    workspace треда и показывается вложением.

    Создаётся сборкой чата из держателя контекста, хранилища вложений и
    монтирования ChatMount. Файл пишется в каталог upload треда того хода, в
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

    async def attached(self, call: DagNode, index: int, mime: str, data: bytes) -> str:
        call_id = call.key
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
    def _name(call: DagNode, index: int, mime: str) -> str:
        extension = mimetypes.guess_extension(mime)
        if extension is None:
            extension = ""

        return f"{call.tool}-{uuid4().hex[:8]}-{index}{extension}"
