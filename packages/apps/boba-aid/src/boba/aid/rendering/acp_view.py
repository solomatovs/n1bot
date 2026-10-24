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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeAlias

import mcp_types as mt
from acp.experimental.v2 import schema
from chainlit.step import Step
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter, ValidationError

from boba.aid.rendering.chat_view import ChatView, StepText
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
    IdleErrorUpdate,
    MetaKey,
    StopReason,
    ToolStatus,
)

__all__ = [
    "JournalRegistry",
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


@dataclass(frozen=True)
class TurnEnding:
    """Чем кончился ход по его idle: исход для итога хода в шине и пометка
    ленты. Строит UpdateView.ending — для живого хода держатель (AgentFeed),
    для истории сам переводчик; у штатного конца пометки нет."""

    outcome: TurnOutcome
    note: str


class UpdateView:
    """Перевод уведомлений одной сессии в шаги ленты через ChatView.

    Создаётся рендерером шины (ChatRenderer) на тред и приёмником потока
    вкладки (AgentFeed) на воспроизведение истории. Ход открывается
    `begin_turn`/`question` (живой — по TurnStarted из шины, при
    воспроизведении — по `user_message`), уведомления идут в `apply`, конец
    хода — `finish_turn`. Вызов создаёт и меняет одно уведомление
    `tool_call_update`. idle хода — его конец: остановка хода с шагами —
    подпись контейнера, без шагов — пометка к ответу; сбой встаёт шагом
    ошибки; живой ход получает idle из шины,
    история — из воспроизведения агента.

    Текущее действие хода для подписи контейнера: начатый вызов, у связки —
    её первый узел, а дальше узел, о котором сервер сообщил ход работы или
    открытый журнал.
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
        self._cards = QuestionCards()
        self._lines = QuestionLines()
        self._round_list: TypeAdapter[list[ElicitationRound]] = TypeAdapter(
            list[ElicitationRound]
        )
        self._turn_key: str | None = None
        self._thought_id: str | None = None
        self._answer_id: str | None = None
        self._steps: dict[str, Step] = {}
        self._compactions: dict[str, Step] = {}
        """Идущее сжатие → его шаг: в истории остаётся только состоявшееся."""
        self._asked: dict[str, Mapping[str, Any]] = {}
        """Идущий вызов-вопрос пользователю → его ввод: по нему рисуется
        запись ответа."""
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
        self._compactions.clear()
        self._asked.clear()
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
        вложения; строку вызова команды из SilentCommand — нет."""
        if not self._lines.shown(text):
            return

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

        if isinstance(update, schema.IdleSessionStateUpdate):
            await self._ended(update)
            return

        if isinstance(update, schema.SessionCompactionUpdate):
            await self._compaction(update)
            return

        logger.debug(
            "thread %s: update %s is not drawn", self._thread_id, type(update).__name__
        )

    async def _compaction(self, update: schema.SessionCompactionUpdate) -> None:
        """Сжатие разговора шагом хода: идёт — «сжимаю», состоялось — пересказ
        под шагом (история отдаёт сразу состоявшееся), не удалось или
        прервано — шаг убирается: в истории его нет."""
        key = update.compaction_id
        step = self._compactions.pop(key, None)
        if update.status == CompactionState.IN_PROGRESS.value:
            if step is None:
                step = await self._view.compaction_started(key)

            self._compactions[key] = step
            return

        if update.status != CompactionState.COMPLETED.value:
            if step is not None:
                await self._view.compaction_dropped(step)

            return

        if step is None:
            step = await self._view.compaction_started(key)

        await self._view.compaction_finished(step, self._summary_text(update))

    @staticmethod
    def _summary_text(update: schema.SessionCompactionUpdate) -> str:
        parts: list[str] = []
        for block in update.summary or ():
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

        note = f"the turn ended early: {stop_reason}"
        if isinstance(idle, IdleErrorUpdate):
            if idle.error is not None:
                note = f"the turn failed: {idle.error.message}"

        return TurnEnding(outcome=TurnOutcome.FAILED, note=note)

    async def _ended(self, idle: schema.IdleSessionStateUpdate) -> None:
        """Пометка конца хода: остановка — подписью контейнера или к ответу,
        сбой — шагом ошибки."""
        ending = self.ending(idle)
        if ending.outcome is TurnOutcome.STOPPED:
            await self._stopped(ending)
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

        for key, step in list(self._steps.items()):
            asked = self._asked.pop(key, None)
            if asked is None:
                await self._view.tool_stopped(step)
                continue

            card = self._cards.asked(asked, None, True)
            await self._view.question_finished(step, card, key)

        for compaction in list(self._compactions.values()):
            await self._view.compaction_dropped(compaction)

        self._steps.clear()
        self._compactions.clear()
        self._groups.clear()
        await self._view.finish_turn()
        self._turn_key = None

    async def journal_opened(self, opened: JournalOpened) -> None:
        """Сервер открыл журнал идущего вызова: адрес запоминается, шагу
        вызова (у связки — подшагу узла по ключу узла от автора) встаёт
        кнопка журнала, а узел становится текущим действием хода. Вызова без
        шага в этой ленте, связки без такого узла — ничего: кнопку даст
        ссылка в итоге вызова."""
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
        if opened.node is not None:
            await self._view.follow(step)

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
            group = await self._opened(update, name, self._args(update.raw_input))

        asked = self._asked.get(call_id)
        if asked is not None:
            await self._question_update(update, status, asked)
            return

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

    async def _opened(
        self, update: schema.SessionToolCallUpdate, name: str, args: Mapping[str, Any]
    ) -> tuple[NestedCallsFeature, Sequence[DagNode]] | None:
        """Шаги нового вызова: вопрос пользователю, подшаги узлов вызова со
        вложенными вызовами либо один шаг; итог — узлы вызова или None."""
        call_id = update.tool_call_id
        if self._asks_user(update):
            self._asked[call_id] = args
            await self._close_thinking()
            self._answer_id = None
            card = self._cards.asking(args)
            self._steps[call_id] = await self._view.question_asked(card, call_id)
            return None

        group = await self._nodes_of(name, call_id, args)
        if group is None:
            step = await self._step_of(name, args, call_id)
            await self._view.follow(step)
            return None

        steps: list[Step] = []
        for node in group[1]:
            step = await self._step_of(node.tool, node.args, node.key)
            steps.append(step)

        self._groups[call_id] = group
        if steps:
            await self._view.follow(steps[0])

        return group

    async def _question_update(
        self,
        update: schema.SessionToolCallUpdate,
        status: ToolStatus,
        asked: Mapping[str, Any],
    ) -> None:
        """Вызов-вопрос пользователю: ход работы не рисуется, конечный
        статус — запись ответа по вводу и итогу вызова."""
        if not status.terminal:
            return

        call_id = update.tool_call_id
        self._called.pop(call_id, None)
        self._asked.pop(call_id)
        failed = status is not ToolStatus.COMPLETED
        card = self._cards.asked(asked, update.raw_output, failed)
        await self._view.question_finished(self._steps.pop(call_id), card, call_id)

    async def _step_of(self, name: str, args: Mapping[str, Any], key: str) -> Step:
        """Шаг нового вызова инструмента name с аргументами args под ключом key."""
        await self._close_thinking()
        self._answer_id = None
        media: Mapping[str, str] | None = None
        app: ToolAppView | None = None
        card = await self._media.card(name)
        if card is not None:
            media = self._media.media(card)
            app = self._media.app(name, card)

        step = await self._view.tool_started(name, args, key, media, app)
        self._steps[key] = step

        return step

    async def _nodes_of(
        self, name: str, call_id: str, args: Mapping[str, Any]
    ) -> tuple[NestedCallsFeature, Sequence[DagNode]] | None:
        """Объявление сервера и узлы вызова call_id инструмента
        name под ключами подшагов, с полными именами инструментов, как их
        называет ретранслятор; None — сервер вложенных вызовов для name не
        объявил либо вызов не по объявленной форме, и он рисуется одним
        шагом."""
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
            await self._view.follow(step)

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
            _, journals = await self._journaled(reply, name)
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
        `rawOutput`; без него отменённый вызов — остановка, сорванный —
        отказ, прочий — текст уведомления; rawOutput другого вида — как
        есть."""
        call_id = update.tool_call_id
        reply = self._reply(update)
        if reply is not None:
            shown, journals = await self._journaled(reply, name)
            # журналов несколько только у связки: любой из них — чужого узла
            journal: JournalAddress | None = None
            if len(journals) == 1:
                (journal,) = journals.values()

            elapsed = self._elapsed(reply)
            await self._finished(step, name, call_id, journal, shown, elapsed)
            if cards := self._rounds(reply, call_id):
                await self._view.questions_answered(cards, call_id)

            return

        if status is ToolStatus.CANCELLED:
            await self._view.tool_stopped(step)
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

    async def _journaled(
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
                self._thread_id,
                call_id,
                exc,
            )
            return []

        cards: list[QuestionCard] = []
        for each in asked:
            cards.append(self._cards.round(each))

        return cards

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
