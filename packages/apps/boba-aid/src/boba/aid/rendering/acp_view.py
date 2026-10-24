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
уведомлением сервера в потоке вызова. Вызов инструмента, который сервер
объявил несущим вложенные вызовы (NestedCallsFeature), рисуется шагами его
узлов: ход узла приходит с ключом узла в `_meta`, итог узла — его блоки по
записи списка узлов в structuredContent (NestedCallsOutput). У инструмента
с приложением MCP Apps (секция apps сервера и _meta.ui.resourceUri) под
контейнером хода встаёт карточка приложения с аргументами и итогом
вызова, а шаг остаётся фактом вызова. Вызов, который агент пометил
вопросом пользователю (`_meta` MetaKey.QUESTION первого уведомления), —
своё сообщение основной ленты с элементом Question: пока вопрос ждёт
ответа — строка «Question(s)» с
мигающей точкой, после итога — запись ответа той же панелью только для
чтения; вопросы сервера MCP, заданные в вызове (раунды в `_meta`
результата), — такими же записями рядом с итогом вызова. Строку вызова
команды, итог которой ход рисует своим шагом (`/compact` — шаг сжатия),
лента не рисует ни вживую, ни при воспроизведении: агент отдаёт запись
вызова тем же текстом, что набрал пользователь (QuestionLines).

Ошибки:
ValidationError — тело уведомления из шины не по схеме ACP v2.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, TypeAlias, get_args

import mcp_types as mt
from acp.experimental.v2 import schema
from chainlit.step import Step
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter, ValidationError

from boba.aid.rendering.chat_view import ChatView, NoticeLevel, StepText
from boba.aid.rendering.question import QuestionCard, QuestionCards
from boba.aid.rendering.tool import AppMeta, ToolAppView
from boba.core.commands import CommandLines
from boba.core.mcp_names import McpToolNames
from boba.mcp_client.client import (
    JournalLinks,
    McpCaller,
    McpClientError,
    McpDescription,
    McpServerApps,
    McpServers,
)
from boba.messaging import JournalOpened, TurnOutcome
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
from boba.toolkit.wire import ResultWire
from boba.transport.acp.protocol import (
    ElicitationRound,
    MetaKey,
    StopReason,
    ToolStatus,
)

__all__ = [
    "CallPainter",
    "CallParts",
    "JournalRegistry",
    "NestedCall",
    "PlainCall",
    "QuestionCall",
    "ServerCards",
    "SessionUpdate",
    "SessionUpdateCodec",
    "ToolCards",
    "ToolMedia",
    "TurnEnding",
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
    | schema.IdleSessionStateUpdate
    | schema.RequiresActionSessionStateUpdate
    | schema.OtherSessionStateUpdate
)
"""Уведомление `session/update` v2."""


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


class ToolCards(Protocol):
    """Откуда лента треда берёт карточки инструментов и описания серверов
    профиля. Реализация — ServerCards: порты McpServers владельца треда."""

    @abstractmethod
    async def card(self, server: str, tool: str) -> mt.Tool | None:
        """Карточка инструмента tool сервера server; None — такого у
        сервера нет либо сервер не ответил."""

    @abstractmethod
    async def described(self, server: str) -> McpDescription | None:
        """Описание сервера server; None — сервер не назван либо не
        ответил."""

    @abstractmethod
    def apps(self, server: str) -> McpServerApps | None:
        """Секция apps сервера server; None — его приложения не рисуются."""


class ServerCards(ToolCards):
    """Реализация ToolCards портами McpServers пользователя, чей это тред.

    Создаётся на тред тем, кто рисует его ленту (приёмник агента вкладки,
    рендерер шины треда). Карточку и описание даёт порт сервера через свой
    кэш ответов: свежие по сроку сервера и из списка только этого
    пользователя — списки с cacheScope private у разных пользователей не
    смешиваются. Сервер, который не ответил, — вызов рисуется без карточки,
    причина в логе.
    """

    def __init__(self, servers: McpServers, caller: McpCaller) -> None:
        self._servers = servers
        self._caller = caller

    async def card(self, server: str, tool: str) -> mt.Tool | None:
        if not self._servers.served(server):
            return None

        port = await self._servers.port(server, self._caller)
        try:
            listed = await port.tools(McpServers.OWN)
        except (McpClientError, MCPError) as exc:
            logger.warning(
                "mcp server %s gave no tool list to %s, tool %s is drawn "
                "without its card: %s",
                server,
                self._caller.login,
                tool,
                exc,
            )
            return None

        for listed_tool in listed.tools:
            if listed_tool.name == tool:
                return listed_tool

        return None

    async def described(self, server: str) -> McpDescription | None:
        if not self._servers.served(server):
            return None

        port = await self._servers.port(server, self._caller)
        try:
            return await port.described(McpServers.OWN)
        except (McpClientError, MCPError) as exc:
            logger.warning(
                "mcp server %s gave no description to %s, its calls are drawn "
                "without nested calls and journals: %s",
                server,
                self._caller.login,
                exc,
            )
            return None

    def apps(self, server: str) -> McpServerApps | None:
        return self._servers.apps(server)


class ToolMedia:
    """Что лента треда знает об инструментах серверов профиля по полным
    именам: типы содержимого аргументов (по ним рисуется вход шага: sql,
    bash, json), вложенные вызовы, которые объявил сервер (по ним вызов
    рисуется шагами узлов), адреса каналов журнала вызовов сервера (по ним
    у шага кнопка журнала) и приложения MCP Apps инструментов сервера с
    секцией apps (по ним у вызова карточка приложения).

    Создаётся на тред вместе с его лентой (UpdateView); своих списков не
    держит: карточку и описание сервера всякий раз даёт источник карточек
    треда (ToolCards).
    """

    def __init__(self, cards: ToolCards) -> None:
        self._cards = cards
        self._views = CallViews()
        self._names = McpToolNames()
        self._app_meta = AppMeta()

    async def card(self, name: str) -> mt.Tool | None:
        """Карточка инструмента по полному имени; None — имя не инструмента
        сервера либо сервер его не знает."""
        owner = self._names.parse(name)
        if owner is None:
            return None

        return await self._cards.card(owner.server, owner.tool)

    def media(self, card: mt.Tool) -> Mapping[str, str]:
        """Типы аргументов инструмента по его карточке."""
        return self._views.media_of(card.input_schema)

    def app(self, name: str, card: mt.Tool) -> ToolAppView | None:
        """Приложение MCP Apps инструмента name по его карточке card; None —
        у сервера нет секции apps либо у инструмента нет
        _meta.ui.resourceUri."""
        owner = self._names.parse(name)
        if owner is None:
            return None

        apps = self._cards.apps(owner.server)
        if apps is None:
            return None

        try:
            config = self._app_meta.of(card)
        except ValidationError as exc:
            logger.warning(
                "mcp server %s: tool %s carries _meta.ui that is not an MCP "
                "Apps tool config, its calls are drawn without the app: %s",
                owner.server,
                card.name,
                exc,
            )
            return None

        if config is None:
            return None

        if config.resource_uri is None:
            return None

        return ToolAppView(
            server=owner.server,
            tool=card,
            resource_uri=config.resource_uri,
            sandbox_url=str(apps.sandbox_url),
        )

    async def nested(self, name: str) -> NestedCallsFeature | None:
        """Объявление вложенных вызовов для вызова инструмента name; None —
        сервер его не объявил либо name не его инструмент со вложенными
        вызовами."""
        owner = self._names.parse(name)
        if owner is None:
            return None

        described = await self._cards.described(owner.server)
        if described is None:
            return None

        nested = described.nested_calls()
        if nested is None:
            return None

        if nested.tool != owner.tool:
            return None

        return nested

    async def links(self, server: str) -> JournalLinks | None:
        """Ссылки на журнал вызовов сервера; None — сервер журнала не
        объявил."""
        described = await self._cards.described(server)
        if described is None:
            return None

        return described.links()


class JournalRegistry(Protocol):
    """Куда лента кладёт адрес журнала закончившегося вызова; реализует
    RemoteJournals (canvas.remote)."""

    @abstractmethod
    def remember(self, thread_id: str, call_id: str, address: JournalAddress) -> None:
        """Адрес журнала вызова call_id треда."""


class SilentCommand(StrEnum):
    """Команды агента, строку вызова которых лента не рисует: итог такой
    команды — свой шаг хода."""

    COMPACT = "compact"


class QuestionLines:
    """Рисовать ли строку вопроса: вызов команды из SilentCommand — нет.
    Решает по тексту вопроса: вживую это набранный вызов, в истории —
    запись вызова, которую агент отдаёт тем же текстом. Держит его
    переводчик ленты (UpdateView)."""

    def __init__(self) -> None:
        self._lines = CommandLines()
        self._silent = frozenset(SilentCommand)

    def shown(self, text: str) -> bool:
        call = self._lines.parsed(text)
        if call is None:
            return True

        return call.name not in self._silent


class CompactionState(StrEnum):
    """Стадии compaction_update ACP, которые различает лента."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TurnEnding:
    """Чем кончился ход по его idle: исход для итога хода в шине и пометка
    ленты. Строит UpdateView.ending — для живого хода держатель (AgentFeed),
    для истории сам переводчик; у штатного конца пометки нет."""

    outcome: TurnOutcome
    note: str


NestedGroup: TypeAlias = tuple[NestedCallsFeature, Sequence[DagNode]]
"""Объявление сервера о вложенных вызовах и узлы вызова под ключами подшагов."""


class CallParts:
    """Общая механика шагов вызовов инструментов: шаг вызова с типами
    аргументов и карточкой приложения, узлы вызова со вложенными вызовами,
    итог шагу по ответу сервера MCP с кнопкой журнала и записями вопросов
    сервера. Ею пользуются отрисовщики вызовов (CallPainter); создаёт
    переводчик ленты (UpdateView)."""

    def __init__(
        self,
        view: ChatView,
        thread_id: str,
        journals: JournalRegistry,
        media: ToolMedia,
    ) -> None:
        self.view = view
        self.thread_id = thread_id
        self._journals = journals
        self._media = media
        self._names = McpToolNames()
        self.cards = QuestionCards()
        self._round_list: TypeAdapter[list[ElicitationRound]] = TypeAdapter(
            list[ElicitationRound]
        )

    async def step(self, name: str, args: Mapping[str, Any], key: str) -> Step:
        """Шаг нового вызова инструмента name с аргументами args под ключом key."""
        media: Mapping[str, str] | None = None
        app: ToolAppView | None = None
        card = await self._media.card(name)
        if card is not None:
            media = self._media.media(card)
            app = self._media.app(name, card)

        return await self.view.tool_started(name, args, key, media, app)

    async def nested(
        self, name: str, call_id: str, args: Mapping[str, Any]
    ) -> NestedGroup | None:
        """Объявление сервера и узлы вызова call_id инструмента name под
        ключами подшагов, с полными именами инструментов, как их называет
        ретранслятор; None — сервер вложенных вызовов для name не объявил
        либо вызов не по объявленной форме, и он рисуется одним шагом."""
        nested = await self._media.nested(name)
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
                self.thread_id,
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

    async def journal(
        self, opened: JournalOpened, step: Step, tool: str, key: str
    ) -> None:
        """Журнал идущего вызова: адрес запоминается, у шага — кнопка."""
        address = JournalAddress(server=opened.server, run=opened.run, call=opened.call)
        self._journals.remember(self.thread_id, key, address)
        await self.view.tool_journaled(step, tool, key)

    def reply(self, update: schema.SessionToolCallUpdate) -> mt.CallToolResult | None:
        """Ответ сервера MCP в `rawOutput` конечного уведомления; None —
        там не результат tools/call."""
        try:
            return mt.CallToolResult.model_validate(update.raw_output)
        except ValidationError as exc:
            logger.debug(
                "thread %s: rawOutput of call %s is not a tools/call result: %s",
                self.thread_id,
                update.tool_call_id,
                exc,
            )
            return None

    async def result(
        self,
        step: Step,
        update: schema.SessionToolCallUpdate,
        status: ToolStatus,
        name: str,
    ) -> None:
        """Итог вызова инструмента name шагу step: ответ сервера MCP из
        `rawOutput`; без него отменённый вызов — остановка, сорванный —
        отказ, прочий — текст уведомления; rawOutput другого вида — как
        есть."""
        call_id = update.tool_call_id
        reply = self.reply(update)
        if reply is not None:
            shown, journals = await self.journaled(reply, name)
            # журналов несколько только у связки: любой из них — чужого узла
            journal: JournalAddress | None = None
            if len(journals) == 1:
                (journal,) = journals.values()

            elapsed = self._elapsed(reply)
            await self.finished(step, name, call_id, journal, shown, elapsed)
            if cards := self._rounds(reply, call_id):
                await self.view.questions_answered(cards, call_id)

            return

        if status is ToolStatus.CANCELLED:
            await self.view.tool_stopped(step)
            return

        if status is not ToolStatus.COMPLETED:
            await self.view.tool_failed(step, self._failure_text(update))
            return

        raw = update.raw_output
        if raw is None:
            text = mt.TextContent(text=self.content_text(update))
            raw = mt.CallToolResult(content=[text])

        await self.finished(step, name, call_id, None, raw, 0)

    async def finished(  # noqa: PLR0913 — итог шага собирается всеми частями ответа
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
            self._journals.remember(self.thread_id, key, journal)
            await self.view.tool_journaled(step, name, key)

        await self.view.tool_finished(step, reply, key, elapsed_ms)

    async def journaled(
        self, reply: mt.CallToolResult, name: str
    ) -> tuple[mt.CallToolResult, Mapping[str, JournalAddress]]:
        """Ответ инструмента name без ссылок на каналы журнала и адреса
        журналов вызовов по идентификаторам вызовов на сервере: ссылка
        журнала — кнопка у шага, а не блок выхода."""
        owner = self._names.parse(name)
        if owner is None:
            return reply, {}

        links = await self._media.links(owner.server)
        if links is None:
            return reply, {}

        shown: list[mt.ContentBlock] = []
        journals: dict[str, JournalAddress] = {}
        for block in reply.content:
            if not isinstance(block, mt.ResourceLink):
                shown.append(block)
                continue

            read = links.address(str(block.uri))
            if read is None:
                shown.append(block)
                continue

            journals[read.node] = JournalAddress(
                server=owner.server, run=read.run, call=read.node
            )

        return reply.model_copy(update={"content": shown}), journals

    def _rounds(self, reply: mt.CallToolResult, call_id: str) -> list[QuestionCard]:
        """Записи ответов на вопросы сервера MCP, заданные в вызове: раунды
        в `_meta` результата; раунды не по форме — в журнал."""
        meta = reply.meta
        if meta is None:
            return []

        rounds = meta.get(MetaKey.ELICITATIONS.value)
        if rounds is None:
            return []

        try:
            asked = self._round_list.validate_python(rounds)
        except ValidationError as exc:
            logger.warning(
                "thread %s: the question rounds of call %s are not drawn: %s",
                self.thread_id,
                call_id,
                exc,
            )
            return []

        cards: list[QuestionCard] = []
        for each in asked:
            cards.append(self.cards.round(each))

        return cards

    @staticmethod
    def _elapsed(reply: mt.CallToolResult) -> int:
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

    def _failure_text(self, update: schema.SessionToolCallUpdate) -> str:
        text = self.content_text(update)
        if text:
            return text

        return f"tool call {update.tool_call_id} ended {update.status} with no text"

    @staticmethod
    def content_text(update: schema.SessionToolCallUpdate) -> str:
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


class CallPainter(Protocol):
    """Отрисовщик одного вызова инструмента в ленте от первого уведомления
    до итога. Вид отрисовщика выбирает переводчик ленты (UpdateView) по
    первому уведомлению вызова: вопрос пользователю, вызов со вложенными
    вызовами или обычный вызов; дальше уведомления вызова идут в его
    отрисовщик без проверок вида."""

    @abstractmethod
    async def progressed(self, update: schema.SessionToolCallUpdate) -> None:
        """Ход работы вызова."""

    @abstractmethod
    async def finished(
        self, update: schema.SessionToolCallUpdate, status: ToolStatus
    ) -> None:
        """Конечный статус вызова с итогом."""

    @abstractmethod
    async def stopped(self) -> None:
        """Ход кончился, а вызов итога не получил."""

    @abstractmethod
    async def journal_opened(self, opened: JournalOpened) -> None:
        """Сервер открыл журнал идущего вызова."""

    @abstractmethod
    def steps(self) -> Sequence[Step]:
        """Шаги вызова, у которых ещё нет итога."""


class PlainCall(CallPainter):
    """Реализация CallPainter для обычного вызова: один шаг, ход работы —
    строкой шага, итог — ответом сервера."""

    def __init__(self, parts: CallParts, step: Step, name: str, call_id: str) -> None:
        self._parts = parts
        self._step = step
        self._name = name
        self._call_id = call_id

    async def progressed(self, update: schema.SessionToolCallUpdate) -> None:
        progress = self._parts.content_text(update)
        if not progress:
            return

        await self._parts.view.tool_progressed(self._step, progress)

    async def finished(
        self, update: schema.SessionToolCallUpdate, status: ToolStatus
    ) -> None:
        await self._parts.result(self._step, update, status, self._name)

    async def stopped(self) -> None:
        await self._parts.view.tool_stopped(self._step)

    async def journal_opened(self, opened: JournalOpened) -> None:
        if opened.node is not None:
            return

        await self._parts.journal(opened, self._step, self._name, self._call_id)

    def steps(self) -> Sequence[Step]:
        return (self._step,)


class NestedCall(CallPainter):
    """Реализация CallPainter для вызова, который сервер объявил несущим
    вложенные вызовы (NestedCallsFeature): подшаг на узел, ход узла — по
    ключу узла в `_meta`, итог узла — его блоки по записи списка узлов
    (NestedCallsOutput)."""

    def __init__(
        self,
        parts: CallParts,
        name: str,
        call_id: str,
        group: NestedGroup,
        steps: Mapping[str, Step],
    ) -> None:
        self._parts = parts
        self._name = name
        self._call_id = call_id
        self._nested, self._nodes = group
        self._steps = dict(steps)
        """Ключ узла → подшаг, у которого ещё нет итога."""

    async def progressed(self, update: schema.SessionToolCallUpdate) -> None:
        """Ход работы — подшагу узла, чей ключ сервер положил в `_meta` под
        ключом, объявленным расширением; строка без ключа узла этого вызова
        уходит в лог."""
        progress = self._parts.content_text(update)
        if not progress:
            return

        node = self._node_of(update)
        if node is None:
            logger.debug(
                "thread %s: progress of call %s names no node of it: %r",
                self._parts.thread_id,
                update.tool_call_id,
                progress,
            )
            return

        if step := self._steps.get(node.key):
            await self._parts.view.tool_progressed(step, progress)
            await self._parts.view.follow(step)

    async def finished(
        self, update: schema.SessionToolCallUpdate, status: ToolStatus
    ) -> None:
        """Итог каждому подшагу — его блоки ответа по записи в списке узлов,
        журнал — по ссылке на журнал его вызова. Подшаг без записи и все
        подшаги ответа без списка узлов (отказ до старта, не результат
        tools/call, чужой ответ) получают общий итог вызова."""
        entries: dict[str, NestedCallReply] = {}
        journals: Mapping[str, JournalAddress] = {}
        reply = self._parts.reply(update)
        if reply is not None:
            _, journals = await self._parts.journaled(reply, self._name)
            try:
                replied = NestedCallsOutput.model_validate(reply.structured_content)
            except ValidationError as exc:
                logger.info(
                    "thread %s: the result of call %s of %s lists no nodes, "
                    "every node step gets the result of the call: %s",
                    self._parts.thread_id,
                    update.tool_call_id,
                    self._name,
                    exc,
                )
            else:
                for entry in replied.nodes:
                    entries[entry.key] = entry

        for node in self._nodes:
            step = self._steps.pop(node.key)
            entry = entries.get(node.title)
            if entry is None:
                await self._parts.result(step, update, status, self._name)
                continue

            if reply is None:
                continue

            journal = journals.get(entry.call_id)
            blocks: list[mt.ContentBlock] = []
            for index in entry.blocks:
                blocks.append(reply.content[index])

            failed = entry.status is NodeStatus.FAILED
            shown = mt.CallToolResult(content=blocks, is_error=failed)
            await self._parts.finished(
                step, node.tool, node.key, journal, shown, entry.elapsed_ms
            )

    async def stopped(self) -> None:
        for step in self._steps.values():
            await self._parts.view.tool_stopped(step)

        self._steps.clear()

    async def journal_opened(self, opened: JournalOpened) -> None:
        """Журнал узла — подшагу по ключу узла от автора связки; узел
        становится текущим действием хода."""
        if opened.node is None:
            return

        node = self._node_titled(opened.node)
        if node is None:
            return

        step = self._steps.get(node.key)
        if step is None:
            return

        await self._parts.journal(opened, step, node.tool, node.key)
        await self._parts.view.follow(step)

    def steps(self) -> Sequence[Step]:
        return list(self._steps.values())

    def _node_of(self, update: schema.SessionToolCallUpdate) -> DagNode | None:
        """Узел вызова, чей ключ сервер положил в `_meta` уведомления под
        ключом, объявленным расширением вложенных вызовов; None — ключа нет
        или он не называет узел этого вызова."""
        meta = update.field_meta
        if meta is None:
            return None

        title = meta.get(self._nested.progress_key)
        if not isinstance(title, str):
            return None

        return self._node_titled(title)

    def _node_titled(self, title: str) -> DagNode | None:
        """Узел связки с ключом title от автора связки; None — такого нет."""
        for node in self._nodes:
            if node.title == title:
                return node

        return None


class QuestionCall(CallPainter):
    """Реализация CallPainter для вызова, который агент пометил вопросом
    пользователю: своё сообщение ленты с вопросом, ход работы не рисуется,
    конечный статус — запись ответа по вводу и итогу вызова."""

    def __init__(
        self, parts: CallParts, step: Step, args: Mapping[str, Any], call_id: str
    ) -> None:
        self._parts = parts
        self._step = step
        self._args = args
        self._call_id = call_id

    async def progressed(self, update: schema.SessionToolCallUpdate) -> None:
        return

    async def finished(
        self, update: schema.SessionToolCallUpdate, status: ToolStatus
    ) -> None:
        failed = status is not ToolStatus.COMPLETED
        card = self._parts.cards.asked(self._args, update.raw_output, failed)
        await self._parts.view.question_finished(self._step, card, self._call_id)

    async def stopped(self) -> None:
        card = self._parts.cards.asked(self._args, None, True)
        await self._parts.view.question_finished(self._step, card, self._call_id)

    async def journal_opened(self, opened: JournalOpened) -> None:
        return

    def steps(self) -> Sequence[Step]:
        return (self._step,)


class UpdateView:
    """Перевод уведомлений одной сессии в шаги ленты через ChatView.

    Создаётся рендерером шины (ChatRenderer) на тред и приёмником потока
    вкладки (AgentFeed) на воспроизведение истории. Ход открывается
    `begin_turn`/`question` (живой — по TurnStarted из шины, при
    воспроизведении — по `user_message`), уведомления идут в `apply`, конец
    хода — `finish_turn`. Что рисует каждый вид уведомления, задаёт реестр
    видов: вид без отрисовки перечислен в нём явно (IGNORED), и реестр при
    сборке сверяется со всем перечнем видов `session/update`. Вызов
    инструмента ведёт свой отрисовщик (CallPainter), выбранный по первому
    уведомлению вызова. idle хода — его конец: остановка хода с шагами —
    подпись контейнера, без шагов — пометка к ответу; сбой встаёт шагом
    ошибки; живой ход получает idle из шины, история — из воспроизведения
    агента.

    Текущее действие хода для подписи контейнера: начатый вызов, у связки —
    её первый узел, а дальше узел, о котором сервер сообщил ход работы или
    открытый журнал.
    """

    EARLY: ClassVar[Mapping[str, StepText]] = {
        StopReason.MAX_TOKENS: StepText.ENDED_MAX_TOKENS,
        StopReason.MAX_TURN_REQUESTS: StepText.ENDED_MAX_TURNS,
    }
    """Пометки ленты хода, который кончился пределом раньше ответа."""

    IGNORED: ClassVar[frozenset[type[Any]]] = frozenset(
        {
            schema.UserMessageChunk,
            schema.ToolCallContentChunkUpdate,
            schema.SessionTerminalUpdate,
            schema.SessionTerminalOutputChunk,
            schema.SessionPlanUpdate,
            schema.SessionPlanRemovedUpdate,
            schema.AvailableCommandsUpdate,
            schema.ConfigOptionUpdate,
            schema.SessionInfoUpdate,
            schema.UsageUpdate,
            schema.SessionCompactionSummaryChunk,
            schema.OtherSessionUpdate,
            schema.RunningSessionStateUpdate,
            schema.RequiresActionSessionStateUpdate,
            schema.OtherSessionStateUpdate,
        }
    )
    """Виды уведомлений, которых в ленте нет: план, команды, настройки и
    сведения сессии, заполненность, куски пересказа, состояние идущего хода,
    терминал и неизвестные виды."""

    def __init__(
        self,
        view: ChatView,
        thread_id: str,
        journals: JournalRegistry,
        media: ToolMedia,
    ) -> None:
        self._view = view
        self._thread_id = thread_id
        self._parts = CallParts(view, thread_id, journals, media)
        self._lines = QuestionLines()
        self._turn_key: str | None = None
        self._thought_id: str | None = None
        self._answer_id: str | None = None
        self._calls: dict[str, CallPainter] = {}
        """Идущий вызов → его отрисовщик."""
        self._compactions: dict[str, Step] = {}
        """Идущее сжатие → его шаг: в истории остаётся только состоявшееся."""
        self._painters: dict[type[Any], Callable[[Any], Awaitable[None]]] = {
            schema.UserMessageUpdate: self._user_message,
            schema.AgentThoughtChunk: self._thought_chunk,
            schema.AgentThoughtUpdate: self._thought_chunk,
            schema.AgentMessageChunk: self._message_chunk,
            schema.AgentMessageUpdate: self._message_chunk,
            schema.SessionToolCallUpdate: self._tool_update,
            schema.IdleSessionStateUpdate: self._ended,
            schema.SessionNotice: self._notice,
            schema.SessionCompactionUpdate: self._compaction,
        }
        self._check_registry()

    @property
    def view(self) -> ChatView:
        return self._view

    @property
    def turn_key(self) -> str | None:
        """Ключ открытого хода; None — ход не открыт."""
        return self._turn_key

    def open_steps(self) -> Sequence[Step]:
        """Шаги вызовов, у которых ещё нет итога."""
        steps: list[Step] = []
        for painter in self._calls.values():
            steps.extend(painter.steps())

        return steps

    def draws(self, update: SessionUpdate) -> bool:
        """Уведомление рисуется в ленте."""
        return type(update) in self._painters

    def begin_turn(self, key: str) -> None:
        """Открывает ход: его ключ адресует контейнер и ответы."""
        self._turn_key = key
        self._thought_id = None
        self._answer_id = None
        self._calls.clear()
        self._compactions.clear()
        self._view.begin_turn(key)

    async def question(
        self,
        text: str,
        key: str,
        author: str | None = None,
        links: Sequence[schema.ResourceContentBlock] = (),
    ) -> None:
        """Вопрос пользователя: шаг с id, который вопросу дал агент, и его
        вложения; строку вызова команды из SilentCommand — нет."""
        if not self._lines.shown(text):
            return

        await self._view.question(text, key, name=author, links=links)

    async def apply(self, update: SessionUpdate) -> None:
        """Уведомление сессии в ленту отрисовкой его вида; служебные не
        рисуются."""
        paint = self._painters.get(type(update))
        if paint is None:
            logger.debug(
                "thread %s: update %s is not drawn",
                self._thread_id,
                type(update).__name__,
            )
            return

        await paint(update)

    def _check_registry(self) -> None:
        """Каждый вид `session/update` либо рисуется, либо перечислен в
        IGNORED: новый вид протокола не проходит мимо ленты молча."""
        known = set(get_args(SessionUpdate))
        covered = set(self._painters) | set(self.IGNORED)
        if covered == known:
            return

        missing: list[str] = []
        for kind in known - covered:
            missing.append(kind.__name__)

        extra: list[str] = []
        for kind in covered - known:
            extra.append(kind.__name__)

        msg = (
            f"the feed registry of session/update kinds is out of date: "
            f"undrawn and unlisted {sorted(missing)}, listed but unknown "
            f"{sorted(extra)}"
        )
        raise TypeError(msg)

    async def _compaction(self, update: schema.SessionCompactionUpdate) -> None:
        """Сжатие разговора шагом хода по его стадиям — вживую и в истории
        одинаково: идёт — «сжимаю», состоялось — пересказ под шагом, не
        удалось — причина под шагом, прервано — пометка остановки."""
        key = update.compaction_id
        step = self._compactions.pop(key, None)
        if step is None:
            step = await self._view.compaction_started(key)

        if update.status == CompactionState.IN_PROGRESS.value:
            self._compactions[key] = step
            return

        if update.status == CompactionState.COMPLETED.value:
            await self._view.compaction_finished(step, self._summary_text(update))
            return

        if update.status == CompactionState.CANCELLED.value:
            await self._view.compaction_stopped(step)
            return

        error = update.error
        if error is None:
            error = ""

        await self._view.compaction_failed(step, error)

    @staticmethod
    def _summary_text(update: schema.SessionCompactionUpdate) -> str:
        parts: list[str] = []
        summary = update.summary
        if summary is None:
            return ""

        for block in summary:
            if isinstance(block, schema.TextContentBlock):
                parts.append(block.text)

        return "\n\n".join(parts)

    def ending(self, idle: schema.IdleSessionStateUpdate) -> TurnEnding:
        """Исход хода и пометка ленты по его idle."""
        stop_reason = idle.stop_reason
        if stop_reason is None:
            return TurnEnding(outcome=TurnOutcome.OK, note="")

        if stop_reason == StopReason.END_TURN:
            return TurnEnding(outcome=TurnOutcome.OK, note="")

        if stop_reason == StopReason.CANCELLED:
            note = self._view.label(StepText.STOPPED)
            return TurnEnding(outcome=TurnOutcome.STOPPED, note=note)

        # причину отказа агент уже прислал пометкой notice перед idle
        if stop_reason == StopReason.REFUSAL:
            return TurnEnding(outcome=TurnOutcome.FAILED, note="")

        if early := self.EARLY.get(stop_reason):
            note = self._view.label(early)
            return TurnEnding(outcome=TurnOutcome.FAILED, note=note)

        note = self._view.label(StepText.ENDED_EARLY, reason=stop_reason)
        return TurnEnding(outcome=TurnOutcome.FAILED, note=note)

    async def _ended(self, idle: schema.IdleSessionStateUpdate) -> None:
        """Пометка конца хода: остановка — подписью контейнера или к ответу,
        сбой — шагом ошибки."""
        ending = self.ending(idle)
        if ending.outcome is TurnOutcome.STOPPED:
            await self._stopped(ending)
            return

        if not ending.note:
            return

        if ending.outcome is TurnOutcome.FAILED:
            await self._view.error(ending.note, self._turn_key)

    async def _stopped(self, ending: TurnEnding) -> None:
        """Остановка хода: у хода с шагами её показывает подпись контейнера,
        у хода без шагов — пометка к ответу."""
        self._view.turn_stopped()
        if self._view.container_step is not None:
            return

        await self._view.interrupt_answer(ending.note, self._turn_key)

    async def finish_turn(self) -> None:
        """Ход закончен любым исходом: открытые вызовы закрываются, кружок
        ожидания снимается."""
        await self._close_thinking()
        if self._answer_id is not None:
            await self._view.close_answer(self._turn_key)
            self._answer_id = None

        for painter in list(self._calls.values()):
            await painter.stopped()

        for compaction in list(self._compactions.values()):
            await self._view.compaction_stopped(compaction)

        self._calls.clear()
        self._compactions.clear()
        await self._view.finish_turn()
        self._turn_key = None

    async def journal_opened(self, opened: JournalOpened) -> None:
        """Сервер открыл журнал идущего вызова: его отрисовщик ставит шагу
        (у связки — подшагу узла) кнопку журнала. Вызова без шага в этой
        ленте — ничего: кнопку даст ссылка в итоге вызова."""
        painter = self._calls.get(opened.call_id)
        if painter is None:
            return

        await painter.journal_opened(opened)

    async def _user_message(self, update: schema.UserMessageUpdate) -> None:
        """Новый вопрос закрывает прежний ход и открывает свой: в
        воспроизведении и вживую, когда агент вставил вопрос, заданный по
        ходу, — посреди хода на границе шага или после его ответа; живой ход
        продолжается под вопросом с кружком ожидания. Живое эхо вопроса
        открытого хода уже нарисовано по TurnStarted."""
        if update.message_id == self._turn_key:
            return

        live = self._view.turn_live
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
        if live:
            await self._view.await_model()

    async def _thought_chunk(
        self, update: schema.AgentThoughtChunk | schema.AgentThoughtUpdate
    ) -> None:
        """Рассуждение: живой ход шлёт его кусками, воспроизведение — целым
        сообщением; лента пишет и то и другое в шаг сообщения."""
        if self._withdrawn(update):
            if update.message_id == self._thought_id:
                self._thought_id = None
                await self._view.withdraw_thinking()

            return

        text = self._text(update)
        if text is None:
            return

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
        """Ответ: кусками в живом ходе, целым сообщением в воспроизведении;
        пустая замена — агент снял ответ неудачной попытки."""
        if self._withdrawn(update):
            if update.message_id == self._answer_id:
                self._answer_id = None
                await self._view.withdraw_answer()

            return

        text = self._text(update)
        if text is None:
            return

        await self._close_thinking()
        if self._answer_id is not None and self._answer_id != update.message_id:
            await self._view.seal_answer()

        self._answer_id = update.message_id
        await self._view.stream_answer(text, self._turn_key)

    async def _notice(self, notice: schema.SessionNotice) -> None:
        """Пометка агента — своим сообщением хода."""
        detail = ""
        if notice.description:
            detail = notice.description

        await self._close_thinking()
        await self._view.notice(
            NoticeLevel(notice.severity), notice.title, detail, self._turn_key
        )

    @staticmethod
    def _withdrawn(
        update: schema.AgentThoughtChunk
        | schema.AgentThoughtUpdate
        | schema.AgentMessageChunk
        | schema.AgentMessageUpdate,
    ) -> bool:
        """Целое сообщение с пустым содержимым: агент заменил им сообщение
        неудачной попытки."""
        if isinstance(update, schema.AgentThoughtChunk | schema.AgentMessageChunk):
            return False

        return update.content == []

    async def _tool_update(self, update: schema.SessionToolCallUpdate) -> None:
        """Вызов: первое уведомление выбирает отрисовщик вызова и создаёт
        шаги, дальше ход работы и конечный статус идут в него."""
        call_id = update.tool_call_id
        painter = self._calls.get(call_id)
        if painter is None:
            painter = await self._opened(update)
            self._calls[call_id] = painter

        status = ToolStatus.PENDING
        if update.status is not None:
            status = ToolStatus(update.status)

        if not status.terminal:
            await painter.progressed(update)
            return

        del self._calls[call_id]
        await painter.finished(update, status)

    async def _opened(self, update: schema.SessionToolCallUpdate) -> CallPainter:
        """Отрисовщик нового вызова по его первому уведомлению: вопрос
        пользователю, вызов со вложенными вызовами — подшагами его узлов —
        либо один шаг."""
        call_id = update.tool_call_id
        args = self._args(update.raw_input)
        await self._close_thinking()
        self._answer_id = None
        if self._asks_user(update):
            card = self._parts.cards.asking(args)
            step = await self._view.question_asked(card, call_id)
            return QuestionCall(self._parts, step, args, call_id)

        name = self._tool_name(update)
        group = await self._parts.nested(name, call_id, args)
        if group is None:
            step = await self._parts.step(name, args, call_id)
            await self._view.follow(step)
            return PlainCall(self._parts, step, name, call_id)

        steps: dict[str, Step] = {}
        for node in group[1]:
            steps[node.key] = await self._parts.step(node.tool, node.args, node.key)

        ordered = list(steps.values())
        if ordered:
            await self._view.follow(ordered[0])

        return NestedCall(self._parts, name, call_id, group, steps)

    async def _close_thinking(self) -> None:
        if self._thought_id is None:
            return

        await self._view.close_thinking()
        self._thought_id = None

    def _tool_name(self, update: schema.SessionToolCallUpdate) -> str:
        """Имя инструмента из первого уведомления вызова; без имени шаг
        называется заголовком вызова."""
        if update.name:
            return update.name

        if update.title:
            return update.title

        return self._view.label(StepText.TOOL)

    @staticmethod
    def _asks_user(update: schema.SessionToolCallUpdate) -> bool:
        """Агент пометил вызов вопросом пользователю в `_meta` уведомления."""
        if update.field_meta is None:
            return False

        return update.field_meta.get(MetaKey.QUESTION.value) is True

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
