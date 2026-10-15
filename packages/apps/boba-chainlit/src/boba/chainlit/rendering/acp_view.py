"""Лента чата из уведомлений ACP v2: перевод `session/update` в шаги ChatView.

Один переводчик (UpdateView) рисует и живой ход, чьи уведомления приходят из
шины от держателя, и историю, которую `session/resume` воспроизводит тому же
процессу: живая лента и история — один компонент по одним данным.
Идентификаторы шагов выводятся из `messageId` и `toolCallId` уведомлений
теми же правилами ChatView.derive_id, что и у сохранённой истории:
вопрос и его ответы — id вопроса (`"{id}#{n}"` у n-го ответа хода),
рассуждения — id сообщения ассистента, вызов, график и элемент — id вызова.
Итог вызова — ответ сервера MCP в `rawOutput`: у сервера boba-mcp он
оживает моделью результата с адресом журнала. Вызов инструмента, который
сервер объявил несущим вложенные вызовы (NestedCallsFeature), рисуется
шагами его узлов: ход узла приходит с ключом узла в `_meta`, итог узла —
записью списка узлов в structuredContent (NestedCallsReply).

Ошибки:
ValidationError — тело уведомления из шины не по схеме ACP v2.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, ClassVar, Protocol

import mcp_types as mt
from acp.experimental.v2 import schema
from pydantic import TypeAdapter, ValidationError

from boba.chainlit.agent.client import SessionUpdate
from boba.chainlit.rendering.chat_view import ChatView, StepText
from boba.toolkit.calls import CallViews
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NestedCallReply,
    NestedCallsFeature,
    NodeOutcome,
)
from boba.toolkit.mcp import McpToolNames
from boba.toolkit.result import MarkdownResult
from boba.toolkit.wire import NestedCallsReply, ResultWire, WireResult
from boba.transport.acp.protocol import ToolStatus
from chainlit.step import Step

__all__ = [
    "JournalRegistry",
    "SessionUpdateCodec",
    "ToolMedia",
    "UpdateView",
]

logger = logging.getLogger(__name__)


class SessionUpdateCodec:
    """Уведомление ACP туда и обратно в JSON шины: держатель кладёт его в
    сообщение AgentUpdate, рендерер читает. Литералы вида (`sessionUpdate`)
    различают классы SDK сами."""

    def __init__(self) -> None:
        self._adapter: TypeAdapter[SessionUpdate] = TypeAdapter(SessionUpdate)

    def encode(self, update: SessionUpdate) -> dict[str, Any]:
        return update.model_dump(mode="json", by_alias=True, exclude_none=True)

    def decode(self, raw: Any) -> SessionUpdate:
        """Ошибки:
        ValidationError — тело не проходит схему уведомления.
        """
        return self._adapter.validate_python(raw)


class ToolMedia:
    """Что лента знает об инструментах серверов профиля по полным именам:
    типы содержимого аргументов (по ним рисуется вход шага: sql, bash, json)
    и вложенные вызовы, которые объявил сервер (по ним вызов рисуется шагами
    узлов). Один объект на процесс; инструменты называет сборка приёмника по
    портам серверов профиля."""

    def __init__(self) -> None:
        self._views = CallViews()
        self._names = McpToolNames()
        self._media: dict[str, Mapping[str, str]] = {}
        self._nested: dict[str, NestedCallsFeature] = {}

    def shown(
        self,
        server: str,
        tools: Iterable[mt.Tool],
        nested: NestedCallsFeature | None,
    ) -> None:
        """Типы аргументов инструментов tools сервера server под полными
        именами и вложенные вызовы, объявленные сервером; None — сервер их
        не объявил."""
        for tool in tools:
            name = self._names.full(server, tool.name)
            self._media[name] = self._views.media_of(tool.input_schema)

        if nested is None:
            return

        self._nested[self._names.full(server, nested.tool)] = nested

    def of(self, name: str) -> Mapping[str, str] | None:
        """Типы аргументов инструмента; None — инструмент серверам неизвестен."""
        return self._media.get(name)

    def nested(self, name: str) -> NestedCallsFeature | None:
        """Объявление вложенных вызовов для вызова инструмента name; None —
        сервер его не объявил либо name не его инструмент со вложенными
        вызовами."""
        return self._nested.get(name)


class JournalRegistry(Protocol):
    """Куда лента кладёт адрес журнала закончившегося вызова; реализует
    RemoteJournals (canvas.remote)."""

    @abstractmethod
    def remember(self, thread_id: str, call_id: str, address: JournalAddress) -> None:
        """Адрес журнала вызова call_id треда."""


class UpdateView:
    """Перевод уведомлений одной сессии в шаги ленты через ChatView.

    Создаётся рендерером шины (ChatRenderer) на тред и приёмником потока
    вкладки (AgentFeed) на воспроизведение истории. Ход открывается
    `begin_turn`/`question` (живой — по TurnStarted из шины, при
    воспроизведении — по `user_message`), уведомления идут в `apply`, конец
    хода — `finish_turn`. Вызов создаёт и меняет одно уведомление
    `tool_call_update`; его итог (outcome) нужен и держателю хода
    (ResultMounts).
    """

    STEP_FALLBACK_NAME: ClassVar[str] = StepText.TOOL.value

    def __init__(
        self,
        view: ChatView,
        thread_id: str,
        journals: JournalRegistry,
        media: ToolMedia,
    ) -> None:
        self._view = view
        self._thread_id = thread_id
        self._journals = journals
        self._media = media
        self._names = McpToolNames()
        self._wire = ResultWire()
        self._turn_key: str | None = None
        self._thought_id: str | None = None
        self._answer_id: str | None = None
        self._steps: dict[str, Step] = {}
        self._called: dict[str, str] = {}
        """Идущий вызов → имя его инструмента: агент называет инструмент
        только в первом уведомлении вызова."""
        self._groups: dict[str, tuple[NestedCallsFeature, str, Sequence[DagNode]]] = {}
        """Идущий вызов со вложенными вызовами → объявление сервера, имя
        сервера и узлы подшагов: ключ узла — ключ подшага, title — ключ узла
        от автора связки."""

    @property
    def view(self) -> ChatView:
        return self._view

    @property
    def turn_key(self) -> str | None:
        """Ключ открытого хода; None — ход не открыт."""
        return self._turn_key

    def open_steps(self) -> Sequence[Step]:
        """Шаги вызовов, у которых ещё нет итога."""
        return list(self._steps.values())

    def begin_turn(self, key: str) -> None:
        """Открывает ход: его ключ адресует контейнер и ответы."""
        self._turn_key = key
        self._thought_id = None
        self._answer_id = None
        self._steps.clear()
        self._called.clear()
        self._groups.clear()
        self._view.begin_turn(key)

    async def question(
        self,
        text: str,
        key: str,
        author: str | None = None,
        links: Sequence[schema.ResourceContentBlock] = (),
    ) -> None:
        """Вопрос пользователя: шаг с id, который вопросу дал агент, и его
        вложения."""
        await self._view.question(text, key, name=author, links=links)

    async def apply(self, update: SessionUpdate) -> None:
        """Уведомление сессии в ленту; служебные не рисуются."""
        if isinstance(update, schema.UserMessageUpdate):
            await self._user_message(update)
            return

        if isinstance(update, schema.AgentThoughtChunk | schema.AgentThoughtUpdate):
            await self._thought_chunk(update)
            return

        if isinstance(update, schema.AgentMessageChunk | schema.AgentMessageUpdate):
            await self._message_chunk(update)
            return

        if isinstance(update, schema.SessionToolCallUpdate):
            await self._tool_update(update)
            return

        logger.debug(
            "thread %s: update %s is not drawn", self._thread_id, type(update).__name__
        )

    async def finish_turn(self) -> None:
        """Ход закончен любым исходом: открытые вызовы закрываются, кружок
        ожидания снимается."""
        await self._close_thinking()
        if self._answer_id is not None:
            await self._view.close_answer(self._turn_key)
            self._answer_id = None

        for step in list(self._steps.values()):
            await self._view.tool_stopped(step, StepText.FINISHED.value)

        self._steps.clear()
        self._groups.clear()
        await self._view.finish_turn()
        self._turn_key = None

    def outcome(
        self, update: schema.SessionToolCallUpdate, name: str
    ) -> NodeOutcome | None:
        """Итог вызова инструмента name из `rawOutput` конечного уведомления
        update — ответа сервера MCP: модель результата и адрес журнала у
        сервера boba-mcp. Имя вызова держит вызывающий: агент называет
        инструмент только в первом уведомлении вызова. None — инструмент не
        сервера MCP, ответ не CallToolResult либо сервер модели результата
        не прислал."""
        owner = self._names.parse(name)
        if owner is None:
            return None

        reply = self._reply(update)
        if reply is None:
            return None

        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        call = DagNode(key=update.tool_call_id, tool=name)
        try:
            return self._wire.revived(wire, call, owner.server)
        except ValidationError as exc:
            logger.warning(
                "thread %s: the result of call %s of %s names a known kind but "
                "does not match its model, shown as text: %s",
                self._thread_id,
                update.tool_call_id,
                name,
                exc,
            )
            return None

    def _reply(self, update: schema.SessionToolCallUpdate) -> mt.CallToolResult | None:
        """Ответ сервера MCP в `rawOutput` конечного уведомления; None —
        там не результат tools/call."""
        try:
            return mt.CallToolResult.model_validate(update.raw_output)
        except ValidationError as exc:
            logger.debug(
                "thread %s: rawOutput of call %s is not a tools/call result: %s",
                self._thread_id,
                update.tool_call_id,
                exc,
            )
            return None

    async def _user_message(self, update: schema.UserMessageUpdate) -> None:
        """Вопрос из воспроизведения закрывает прежний ход и открывает свой;
        живое эхо вопроса открытого хода уже нарисовано по TurnStarted."""
        if update.message_id == self._turn_key:
            return

        if self._turn_key is not None:
            await self.finish_turn()

        text = self._text(update)
        if text is None:
            text = ""

        content = update.content
        if content is None:
            content = []

        links: list[schema.ResourceContentBlock] = []
        for block in content:
            if isinstance(block, schema.ResourceContentBlock):
                links.append(block)

        self.begin_turn(update.message_id)
        await self.question(text, update.message_id, links=links)

    async def _thought_chunk(
        self, update: schema.AgentThoughtChunk | schema.AgentThoughtUpdate
    ) -> None:
        """Рассуждение: живой ход шлёт его кусками, воспроизведение — целым
        сообщением; лента пишет и то и другое в шаг сообщения."""
        text = self._text(update)
        if text is None:
            return

        await self._view.model_answered()
        if self._thought_id is not None and self._thought_id != update.message_id:
            await self._close_thinking()

        if self._answer_id is not None and self._answer_id != update.message_id:
            await self._view.seal_answer()
            self._answer_id = None

        self._thought_id = update.message_id
        await self._view.stream_thinking(text, update.message_id)

    async def _message_chunk(
        self, update: schema.AgentMessageChunk | schema.AgentMessageUpdate
    ) -> None:
        """Ответ: кусками в живом ходе, целым сообщением в воспроизведении."""
        text = self._text(update)
        if text is None:
            return

        await self._view.model_answered()
        await self._close_thinking()
        if self._answer_id is not None and self._answer_id != update.message_id:
            await self._view.seal_answer()

        self._answer_id = update.message_id
        await self._view.stream_answer(text, self._turn_key)

    async def _tool_update(self, update: schema.SessionToolCallUpdate) -> None:
        """Вызов: первое уведомление создаёт шаг (у вызова со вложенными
        вызовами — подшаги узлов), ход работы — текст `content`, конечный
        статус — итог."""
        call_id = update.tool_call_id
        name = self._tool_name(update)
        status = ToolStatus.PENDING
        if update.status is not None:
            status = ToolStatus(update.status)

        group = self._groups.get(call_id)
        if group is None and call_id not in self._steps:
            args = self._args(update.raw_input)
            group = self._nodes_of(name, call_id, args)
            if group is None:
                await self._step_of(name, args, call_id)
            else:
                for node in group[2]:
                    await self._step_of(node.tool, node.args, node.key)

                self._groups[call_id] = group

        if not status.terminal:
            progress = self._content_text(update)
            if not progress:
                return

            if group is None:
                await self._view.tool_progressed(self._steps[call_id], progress)
                return

            await self._node_progress(update, group, progress)
            return

        self._called.pop(call_id, None)
        if group is None:
            await self._tool_result(self._steps.pop(call_id), update, status, name)
            return

        self._groups.pop(call_id)
        await self._nodes_result(update, name, group, status)

    async def _step_of(self, name: str, args: Mapping[str, Any], key: str) -> Step:
        """Шаг нового вызова инструмента name с аргументами args под ключом key."""
        await self._close_thinking()
        self._answer_id = None
        step = await self._view.tool_started(name, args, key, self._media.of(name))
        self._steps[key] = step

        return step

    def _nodes_of(
        self, name: str, call_id: str, args: Mapping[str, Any]
    ) -> tuple[NestedCallsFeature, str, Sequence[DagNode]] | None:
        """Объявление сервера, имя сервера и узлы вызова call_id инструмента
        name под ключами подшагов, с полными именами инструментов, как их
        называет ретранслятор; None — сервер вложенных вызовов для name не
        объявил либо вызов не по объявленной форме, и он рисуется одним
        шагом."""
        nested = self._media.nested(name)
        if nested is None:
            return None

        owner = self._names.parse(name)
        if owner is None:
            return None

        try:
            calls = nested.calls(args)
        except StreamPlanError as exc:
            logger.debug(
                "thread %s: call %s of %s is drawn as one step: %s",
                self._thread_id,
                call_id,
                name,
                exc,
            )
            return None

        nodes: list[DagNode] = []
        for node in nested.keyed(call_id, calls):
            full = self._names.full(owner.server, node.tool)
            nodes.append(node.model_copy(update={"tool": full}))

        return nested, owner.server, nodes

    async def _node_progress(
        self,
        update: schema.SessionToolCallUpdate,
        group: tuple[NestedCallsFeature, str, Sequence[DagNode]],
        progress: str,
    ) -> None:
        """Ход работы progress — подшагу узла, чей ключ сервер положил в
        `_meta` под ключом, объявленным расширением; строка без ключа узла
        этого вызова уходит в лог."""
        nested, _, nodes = group
        title = None
        if meta := update.field_meta:
            title = meta.get(nested.progress_key)

        for node in nodes:
            if node.title != title:
                continue

            if step := self._steps.get(node.key):
                await self._view.tool_progressed(step, progress)

            return

        logger.debug(
            "thread %s: progress of call %s names no node of it (%r): %r",
            self._thread_id,
            update.tool_call_id,
            title,
            progress,
        )

    async def _nodes_result(
        self,
        update: schema.SessionToolCallUpdate,
        name: str,
        group: tuple[NestedCallsFeature, str, Sequence[DagNode]],
        status: ToolStatus,
    ) -> None:
        """Итог каждому подшагу по его записи в списке узлов ответа
        (NestedCallsReply): запуск у узлов общий, журнал у каждого свой.
        Подшаг без записи и все подшаги ответа без списка узлов (отказ до
        старта, не результат tools/call, чужой ответ) получают общий итог
        вызова."""
        _, server, nodes = group
        entries: dict[str, NestedCallReply] = {}
        run = ""
        reply = self._reply(update)
        if reply is not None:
            try:
                replied = NestedCallsReply.model_validate(reply.structured_content)
            except ValidationError as exc:
                logger.info(
                    "thread %s: the result of call %s of %s lists no nodes, "
                    "every node step gets the result of the call: %s",
                    self._thread_id,
                    update.tool_call_id,
                    name,
                    exc,
                )
            else:
                run = replied.call.run
                for entry in replied.nodes:
                    entries[entry.key] = entry

        for node in nodes:
            step = self._steps.pop(node.key)
            entry = entries.get(node.title)
            if entry is None:
                await self._tool_result(step, update, status, name)
                continue

            journal = None
            if run:
                journal = JournalAddress(server=server, run=run, call=entry.call_id)

            await self._finished(step, node.key, journal, entry.result)

    async def _tool_result(
        self,
        step: Step,
        update: schema.SessionToolCallUpdate,
        status: ToolStatus,
        name: str,
    ) -> None:
        """Итог вызова инструмента name шагу step."""
        call_id = update.tool_call_id
        outcome = self.outcome(update, name)
        if outcome is not None:
            await self._finished(step, call_id, outcome.journal, outcome.artifact)
            return

        if status is not ToolStatus.COMPLETED:
            self._view.drop_journal(step)
            await self._view.tool_failed(step, self._failure_text(update))
            return

        text = MarkdownResult(text=self._content_text(update))
        await self._finished(step, call_id, None, text)

    async def _finished(
        self, step: Step, key: str, journal: JournalAddress | None, artifact: Any
    ) -> None:
        """Итог шагу step с ключом key: кнопка журнала по адресу journal
        (None — журнала нет) и показ результата artifact."""
        if journal is None:
            self._view.drop_journal(step)
        else:
            self._journals.remember(self._thread_id, key, journal)

        await self._view.tool_finished(step, artifact, key)

    async def _close_thinking(self) -> None:
        if self._thought_id is None:
            return

        await self._view.close_thinking()
        self._thought_id = None

    def _failure_text(self, update: schema.SessionToolCallUpdate) -> str:
        text = self._content_text(update)
        if text:
            return text

        return f"tool call {update.tool_call_id} ended {update.status} with no text"

    @staticmethod
    def _content_text(update: schema.SessionToolCallUpdate) -> str:
        if update.content is None:
            return ""

        parts: list[str] = []
        for item in update.content:
            if not isinstance(item, schema.ContentToolCallContent):
                continue

            block = item.content
            if isinstance(block, schema.TextContentBlock):
                parts.append(block.text)

        return "\n".join(parts)

    def _tool_name(self, update: schema.SessionToolCallUpdate) -> str:
        """Имя инструмента вызова из его первого уведомления; без имени шаг
        называется заголовком вызова."""
        if update.name:
            self._called[update.tool_call_id] = update.name
            return update.name

        known = self._called.get(update.tool_call_id)
        if known is not None:
            return known

        if update.title:
            return update.title

        return self.STEP_FALLBACK_NAME

    @staticmethod
    def _args(raw_input: Any) -> Mapping[str, Any]:
        if isinstance(raw_input, Mapping):
            return raw_input

        return {}

    @staticmethod
    def _text(
        update: schema.AgentThoughtChunk
        | schema.AgentMessageChunk
        | schema.AgentThoughtUpdate
        | schema.AgentMessageUpdate
        | schema.UserMessageUpdate,
    ) -> str | None:
        """Текст куска либо целого сообщения агента или вопроса; картинки и
        ресурсы лента не показывает. None — текста нет."""
        content = update.content
        if isinstance(content, schema.TextContentBlock):
            return content.text

        if not isinstance(content, list):
            return None

        parts: list[str] = []
        for block in content:
            if isinstance(block, schema.TextContentBlock):
                parts.append(block.text)

        if not parts:
            return None

        return "".join(parts)
