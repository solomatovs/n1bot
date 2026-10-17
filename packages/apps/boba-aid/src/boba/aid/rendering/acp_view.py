"""Лента чата из уведомлений ACP v2: перевод `session/update` в шаги ChatView.

Один переводчик (UpdateView) рисует и живой ход, чьи уведомления приходят из
шины от держателя, и историю, которую `session/resume` воспроизводит тому же
процессу: живая лента и история — один компонент по одним данным.
Идентификаторы шагов выводятся из `messageId` и `toolCallId` уведомлений
теми же правилами ChatView.derive_id, что и у сохранённой истории:
вопрос и его ответы — id вопроса (`"{id}#{n}"` у n-го ответа хода),
рассуждения — id сообщения ассистента, вызов, график и элемент — id вызова.
Итог вызова — ответ сервера MCP в `rawOutput`: его блоки рисует лента по
виду и MIME, ссылки на каналы журнала (по шаблону расширения журнала
сервера) дают адрес журнала вызова. Журнал идущего вызова лента узнаёт
сообщением шины JournalOpened: его шлёт ретранслятор чата, узнав журнал
у сервера ресурсом resources/list. Вызов инструмента, который сервер
объявил несущим вложенные вызовы (NestedCallsFeature), рисуется шагами его
узлов: ход узла приходит с ключом узла в `_meta`, итог узла — его блоки по
записи списка узлов в structuredContent (NestedCallsOutput). У инструмента
с приложением MCP Apps (секция apps сервера и _meta.ui.resourceUri) шаг
получает элемент приложения с аргументами и итогом вызова.

Ошибки:
ValidationError — тело уведомления из шины не по схеме ACP v2.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol, TypeAlias

import mcp_types as mt
from acp.experimental.v2 import schema
from chainlit.step import Step
from pydantic import TypeAdapter, ValidationError

from boba.aid.rendering.chat_view import ChatView, StepText
from boba.aid.rendering.tool import AppMeta, ToolAppView
from boba.mcp_client.client import JournalLinks, McpServerApps
from boba.messaging import JournalOpened
from boba.toolkit.calls import CallViews
from boba.toolkit.chain import StreamPlanError
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NestedCallReply,
    NestedCallsFeature,
    NestedCallsOutput,
    NodeStatus,
)
from boba.toolkit.mcp import McpToolNames
from boba.toolkit.wire import ResultWire
from boba.transport.acp.protocol import IdleErrorUpdate, ToolStatus

__all__ = [
    "JournalRegistry",
    "SessionUpdate",
    "SessionUpdateCodec",
    "ToolMedia",
    "UpdateView",
]

logger = logging.getLogger(__name__)


SessionUpdate: TypeAlias = (
    schema.UserMessageChunk
    | schema.UserMessageUpdate
    | schema.AgentMessageChunk
    | schema.AgentMessageUpdate
    | schema.AgentThoughtChunk
    | schema.AgentThoughtUpdate
    | schema.ToolCallContentChunkUpdate
    | schema.SessionToolCallUpdate
    | schema.SessionTerminalUpdate
    | schema.SessionTerminalOutputChunk
    | schema.SessionPlanUpdate
    | schema.SessionPlanRemovedUpdate
    | schema.AvailableCommandsUpdate
    | schema.ConfigOptionUpdate
    | schema.SessionInfoUpdate
    | schema.UsageUpdate
    | schema.SessionNotice
    | schema.SessionCompactionUpdate
    | schema.SessionCompactionSummaryChunk
    | schema.OtherSessionUpdate
    | schema.RunningSessionStateUpdate
    | IdleErrorUpdate
    | schema.IdleSessionStateUpdate
    | schema.RequiresActionSessionStateUpdate
    | schema.OtherSessionStateUpdate
)
"""Уведомление `session/update` v2; idle с ошибкой — до обычного idle, чтобы
разбор не терял его поле error."""


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
    типы содержимого аргументов (по ним рисуется вход шага: sql, bash, json),
    вложенные вызовы, которые объявил сервер (по ним вызов рисуется шагами
    узлов), ссылки на журнал вызовов сервера (по ним у шага кнопка
    журнала) и приложения MCP Apps инструментов сервера с секцией apps (по
    ним у шага приложение). Один объект на процесс; инструменты называет
    сборка приёмника по портам серверов профиля."""

    def __init__(self) -> None:
        self._views = CallViews()
        self._names = McpToolNames()
        self._app_meta = AppMeta()
        self._media: dict[str, Mapping[str, str]] = {}
        self._nested: dict[str, NestedCallsFeature] = {}
        self._journals: dict[str, JournalLinks] = {}
        self._apps: dict[str, ToolAppView] = {}

    def shown(
        self,
        server: str,
        tools: Iterable[mt.Tool],
        nested: NestedCallsFeature | None,
        journal: JournalLinks | None,
        apps: McpServerApps | None,
    ) -> None:
        """Типы аргументов инструментов tools сервера server под полными
        именами, вложенные вызовы и адреса журнала вызовов, объявленные
        сервером, и приложения его инструментов по секции apps; None —
        сервер их не объявил либо секции apps у него нет."""
        for tool in tools:
            name = self._names.full(server, tool.name)
            self._media[name] = self._views.media_of(tool.input_schema)
            if apps is None:
                continue

            try:
                config = self._app_meta.of(tool)
            except ValidationError as exc:
                logger.warning(
                    "mcp server %s: tool %s carries _meta.ui that is not an MCP "
                    "Apps tool config, its calls are drawn without the app: %s",
                    server,
                    tool.name,
                    exc,
                )
                continue

            if config is None:
                continue

            if config.resource_uri is None:
                continue

            self._apps[name] = ToolAppView(
                server=server,
                tool=tool.name,
                resource_uri=config.resource_uri,
                sandbox_url=str(apps.sandbox_url),
            )

        if journal is not None:
            self._journals[server] = journal

        if nested is None:
            return

        self._nested[self._names.full(server, nested.tool)] = nested

    def journal(self, server: str) -> JournalLinks | None:
        """Ссылки на журнал вызовов сервера; None — сервер журнала не
        объявил."""
        return self._journals.get(server)

    def journal_address(self, server: str, uri: str) -> JournalAddress | None:
        """Адрес журнала вызова по ссылке uri на канал журнала сервера
        server; None — сервер журнала не объявил либо ссылка не на журнал."""
        uris = self._journals.get(server)
        if uris is None:
            return None

        read = uris.address(uri)
        if read is None:
            return None

        return JournalAddress(server=server, run=read.run, call=read.node)

    def of(self, name: str) -> Mapping[str, str] | None:
        """Типы аргументов инструмента; None — инструмент серверам неизвестен."""
        return self._media.get(name)

    def app(self, name: str) -> ToolAppView | None:
        """Приложение MCP Apps инструмента name; None — у сервера нет секции
        apps либо у инструмента нет _meta.ui.resourceUri."""
        return self._apps.get(name)

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
    `tool_call_update`.
    """

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
        self._turn_key: str | None = None
        self._thought_id: str | None = None
        self._answer_id: str | None = None
        self._steps: dict[str, Step] = {}
        self._called: dict[str, str] = {}
        """Идущий вызов → имя его инструмента: агент называет инструмент
        только в первом уведомлении вызова."""
        self._groups: dict[str, tuple[NestedCallsFeature, Sequence[DagNode]]] = {}
        """Идущий вызов со вложенными вызовами → объявление сервера и узлы
        подшагов: ключ узла — ключ подшага, title — ключ узла от автора
        связки."""

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
            await self._view.tool_stopped(step)

        self._steps.clear()
        self._groups.clear()
        await self._view.finish_turn()
        self._turn_key = None

    async def journal_opened(self, opened: JournalOpened) -> None:
        """Сервер открыл журнал идущего вызова: адрес запоминается, шагу
        вызова (у связки — подшагу узла по ключу узла от автора) встаёт
        кнопка журнала. Вызова без шага в этой ленте, связки без такого узла
        — ничего: кнопку даст ссылка в итоге вызова."""
        name = self._called.get(opened.call_id)
        if name is None:
            return

        tool = name
        key = opened.call_id
        if opened.node is not None:
            group = self._groups.get(opened.call_id)
            if group is None:
                return

            node = self._node_titled(group, opened.node)
            if node is None:
                return

            tool = node.tool
            key = node.key

        step = self._steps.get(key)
        if step is None:
            return

        address = JournalAddress(server=opened.server, run=opened.run, call=opened.call)
        self._journals.remember(self._thread_id, key, address)
        await self._view.tool_journaled(step, tool, key)

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
                for node in group[1]:
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
        step = await self._view.tool_started(
            name, args, key, self._media.of(name), self._media.app(name)
        )
        self._steps[key] = step

        return step

    def _nodes_of(
        self, name: str, call_id: str, args: Mapping[str, Any]
    ) -> tuple[NestedCallsFeature, Sequence[DagNode]] | None:
        """Объявление сервера и узлы вызова call_id инструмента
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

        return nested, nodes

    async def _node_progress(
        self,
        update: schema.SessionToolCallUpdate,
        group: tuple[NestedCallsFeature, Sequence[DagNode]],
        progress: str,
    ) -> None:
        """Ход работы progress — подшагу узла, чей ключ сервер положил в
        `_meta` под ключом, объявленным расширением; строка без ключа узла
        этого вызова уходит в лог."""
        node = self._node_of(update, group)
        if node is None:
            logger.debug(
                "thread %s: progress of call %s names no node of it: %r",
                self._thread_id,
                update.tool_call_id,
                progress,
            )
            return

        if step := self._steps.get(node.key):
            await self._view.tool_progressed(step, progress)

    def _node_of(
        self,
        update: schema.SessionToolCallUpdate,
        group: tuple[NestedCallsFeature, Sequence[DagNode]],
    ) -> DagNode | None:
        """Узел вызова, чей ключ сервер положил в `_meta` уведомления под
        ключом, объявленным расширением вложенных вызовов; None — ключа нет
        или он не называет узел этого вызова."""
        nested, _nodes = group
        meta = update.field_meta
        if meta is None:
            return None

        title = meta.get(nested.progress_key)
        if not isinstance(title, str):
            return None

        return self._node_titled(group, title)

    @staticmethod
    def _node_titled(
        group: tuple[NestedCallsFeature, Sequence[DagNode]], title: str
    ) -> DagNode | None:
        """Узел связки с ключом title от автора связки; None — такого нет."""
        _, nodes = group
        for node in nodes:
            if node.title == title:
                return node

        return None

    async def _nodes_result(
        self,
        update: schema.SessionToolCallUpdate,
        name: str,
        group: tuple[NestedCallsFeature, Sequence[DagNode]],
        status: ToolStatus,
    ) -> None:
        """Итог каждому подшагу — его блоки ответа по записи в списке узлов
        (NestedCallsOutput), журнал — по ссылке на журнал его вызова.
        Подшаг без записи и все подшаги ответа без списка узлов (отказ до
        старта, не результат tools/call, чужой ответ) получают общий итог
        вызова."""
        _, nodes = group
        entries: dict[str, NestedCallReply] = {}
        journals: Mapping[str, JournalAddress] = {}
        reply = self._reply(update)
        if reply is not None:
            _, journals = self._journaled(reply, name)
            try:
                replied = NestedCallsOutput.model_validate(reply.structured_content)
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
                for entry in replied.nodes:
                    entries[entry.key] = entry

        for node in nodes:
            step = self._steps.pop(node.key)
            entry = entries.get(node.title)
            if entry is None:
                await self._tool_result(step, update, status, name)
                continue

            if reply is None:
                continue

            journal = journals.get(entry.call_id)
            blocks: list[mt.ContentBlock] = []
            for index in entry.blocks:
                blocks.append(reply.content[index])

            failed = entry.status is NodeStatus.FAILED
            shown = mt.CallToolResult(content=blocks, is_error=failed)
            await self._finished(
                step, node.tool, node.key, journal, shown, entry.elapsed_ms
            )

    async def _tool_result(
        self,
        step: Step,
        update: schema.SessionToolCallUpdate,
        status: ToolStatus,
        name: str,
    ) -> None:
        """Итог вызова инструмента name шагу step: ответ сервера MCP из
        `rawOutput`; без него — текст уведомления, а у сорванного вызова —
        отказ; rawOutput другого вида — как есть."""
        call_id = update.tool_call_id
        reply = self._reply(update)
        if reply is not None:
            shown, journals = self._journaled(reply, name)
            # журналов несколько только у связки: любой из них — чужого узла
            journal: JournalAddress | None = None
            if len(journals) == 1:
                (journal,) = journals.values()

            elapsed = self._elapsed(reply)
            await self._finished(step, name, call_id, journal, shown, elapsed)
            return

        if status is not ToolStatus.COMPLETED:
            await self._view.tool_failed(step, self._failure_text(update))
            return

        raw = update.raw_output
        if raw is None:
            text = mt.TextContent(text=self._content_text(update))
            raw = mt.CallToolResult(content=[text])

        await self._finished(step, name, call_id, None, raw, 0)

    async def _finished(  # noqa: PLR0913 — итог шага собирается всеми частями ответа
        self,
        step: Step,
        name: str,
        key: str,
        journal: JournalAddress | None,
        reply: object,
        elapsed_ms: int,
    ) -> None:
        """Итог шагу step вызова инструмента name с ключом key: кнопка
        журнала по адресу journal (None — журнала нет) и показ ответа
        reply."""
        if journal is not None:
            self._journals.remember(self._thread_id, key, journal)
            await self._view.tool_journaled(step, name, key)

        await self._view.tool_finished(step, reply, key, elapsed_ms)

    def _journaled(
        self, reply: mt.CallToolResult, name: str
    ) -> tuple[mt.CallToolResult, Mapping[str, JournalAddress]]:
        """Ответ инструмента name без ссылок на каналы журнала и адреса
        журналов вызовов по идентификаторам вызовов на сервере: ссылка
        журнала — кнопка у шага, а не блок выхода."""
        owner = self._names.parse(name)
        if owner is None:
            return reply, {}

        shown: list[mt.ContentBlock] = []
        journals: dict[str, JournalAddress] = {}
        for block in reply.content:
            if not isinstance(block, mt.ResourceLink):
                shown.append(block)
                continue

            address = self._media.journal_address(owner.server, str(block.uri))
            if address is None:
                shown.append(block)
                continue

            journals[address.call] = address

        return reply.model_copy(update={"content": shown}), journals

    def _elapsed(self, reply: mt.CallToolResult) -> int:
        """Длительность вызова из _meta ответа; 0 — сервер её не назвал."""
        meta = reply.meta
        if meta is None:
            return 0

        value = meta.get(ResultWire.ELAPSED)
        if not isinstance(value, int):
            return 0

        if value < 0:
            return 0

        return value

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

        return self._view.label(StepText.TOOL)

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
