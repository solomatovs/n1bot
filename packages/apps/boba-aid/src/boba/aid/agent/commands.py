"""Команды «/» вкладки чата: меню поля ввода из трёх источников и маршрут
вызова к владельцу команды.

Команды объявляют сами их владельцы. Агент — по ACP
(`available_commands_update`, AgentCommands); каждый сервер MCP профиля —
своими промптами (prompts/list, смена списка потоком subscriptions/listen,
подсказки значений completion/complete; McpCommands) под полными именами
`mcp__<сервер>__<промпт>`; сам чат — своим реестром с зарезервированными
именами (HarnessCommands). Реестр вкладки CommandMenu собирает их в меню и
ведёт вызов: команду сервера, который согласовал расширение io.boba/commands
и пометил промпт как открывающий приложение, вкладка исполняет сама —
tools/call инструмента и его приложение MCP Apps; команду чата — её
обработчик; всё остальное уходит агенту текстом вопроса `/<имя> <аргументы>`.
Окна, которые команда открыла во вкладке, агент и модель не видят, в
историю они не пишутся.

Ошибки:
CommandError — команду не исполнить: сервер недоступен, аргументы не
    разложить, сервер или чат отказали в вызове.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from uuid import uuid4

import mcp_types as mt
from acp.experimental.v2 import schema
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import PromptsListChanged
from pydantic import ValidationError

from boba.aid.agent.relay import McpRegistration, McpRelay
from boba.aid.rendering.tool import AppMeta, ToolAppView
from boba.connection_broker.tools import GrantedConnections
from boba.connection_broker.user_connections import StoreRef
from boba.connections.stored import ConnectionStoreError
from boba.core.commands import (
    CommandArgument,
    CommandArgumentsError,
    CommandCall,
    CommandLines,
)
from boba.core.mcp_names import McpToolNames
from boba.core.validation import ValidationText
from boba.identity.errors import AuthenticationError
from boba.mcp_client.client import McpCaller, McpClientError, McpServers, McpToolServer
from boba.toolkit.result import TableResult
from boba.toolkit.wire import CommandAction, CommandsFeature, PromptCommand

__all__ = [
    "AgentCommands",
    "AppWindow",
    "CommandBoard",
    "CommandError",
    "CommandIcon",
    "CommandMenu",
    "CommandMenus",
    "CommandSource",
    "HarnessCommand",
    "HarnessCommands",
    "McpCommands",
    "MenuCommand",
]

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """Команду не исполнить; текст — для пользователя вкладки."""


class CommandIcon(StrEnum):
    """Значки lucide команд меню по владельцу команды."""

    AGENT = "sparkles"
    MCP = "server"
    HARNESS = "plug"


@dataclass(frozen=True)
class MenuCommand:
    """Команда меню поля ввода: имя без косой черты, что она делает,
    подсказка аргументов и значок владельца."""

    name: str
    description: str
    hint: str
    icon: CommandIcon


@dataclass(frozen=True)
class AppWindow:
    """Приложение MCP Apps, которое открыла команда сервера: приложение
    инструмента, ключ вызова, его аргументы и итог tools/call по форме MCP."""

    view: ToolAppView
    call_id: str
    arguments: Mapping[str, Any]
    result: Mapping[str, Any]


class CommandBoard(Protocol):
    """Вкладка глазами реестра команд: меню поля ввода, окна команд и шаг
    вопроса-команды. Реализует приёмник потока вкладки AgentFeed (feed.py)."""

    @abstractmethod
    async def offered(self, commands: Sequence[MenuCommand]) -> None:
        """Меню поля ввода вкладки — эти команды, полным списком."""

    @abstractmethod
    async def app_opened(self, window: AppWindow) -> None:
        """Приложение команды сервера — во вкладку, мимо истории агента."""

    @abstractmethod
    async def table_shown(self, title: str, table: TableResult) -> None:
        """Таблица команды чата — во вкладку, мимо истории агента."""

    @abstractmethod
    async def taken(self, step_id: str) -> None:
        """Вопрос-команду исполнила вкладка: его шаг step_id снимается — в
        ленте остаётся только то, что видела модель."""


class CommandSource(Protocol):
    """Владелец команд меню: агент, сервер MCP профиля или сам чат."""

    @abstractmethod
    def commands(self) -> Sequence[MenuCommand]:
        """Команды владельца, которые меню показывает сейчас."""


class AgentCommands(CommandSource):
    """Команды агента вкладки: последний полный список
    `available_commands_update` его сессии. Вызов такой команды —
    обычный вопрос агенту текстом `/<имя> <аргументы>`. Держит его реестр
    вкладки CommandMenu, список ему передаёт агент треда (ThreadAgent)."""

    def __init__(self) -> None:
        self._commands: tuple[MenuCommand, ...] = ()

    def heard(self, update: schema.AvailableCommandsUpdate) -> None:
        listed: list[MenuCommand] = []
        for command in update.available_commands:
            listed.append(
                MenuCommand(
                    name=command.name,
                    description=command.description,
                    hint=self._hint(command),
                    icon=CommandIcon.AGENT,
                )
            )

        self._commands = tuple(listed)

    def commands(self) -> Sequence[MenuCommand]:
        return self._commands

    @staticmethod
    def _hint(command: schema.AvailableCommand) -> str:
        """Подсказка текстового ввода; ввод другого вида меню не рисует."""
        given = command.input
        if not isinstance(given, schema.TextAvailableCommandInput):
            return ""

        return given.hint


@dataclass(frozen=True)
class ServerPrompts:
    """Что сервер MCP сказал о своих промптах при чтении: промпты, подсказки
    аргументов по полному имени команды, расширение команд, если сервер
    его объявил, и обещание смены списка промптов."""

    prompts: Sequence[mt.Prompt]
    hints: Mapping[str, str]
    feature: CommandsFeature | None
    listens: bool


class McpCommands(CommandSource):
    """Команды одного сервера MCP профиля вкладки: его промпты под полными
    именами `mcp__<сервер>__<промпт>`, подсказка — аргументы по порядку и
    значения тех, что сервер подсказывает (completion/complete).

    Создаёт фабрика реестров CommandMenus на каждый сервер профиля. Читает
    сервер от личности чата и от имени пользователя вкладки (read); если
    сервер обещал смену списка промптов, держит поток subscriptions/listen
    и перечитывает список по каждой смене (follow). Сервер, который не
    ответил, — его команд в меню нет, а вызов отказывает с причиной;
    перечитывание после его возвращения вернёт их. Вызов промпта, который
    сервер пометил расширением io.boba/commands как открывающий приложение,
    — tools/call названного инструмента от имени пользователя и его
    приложение во вкладке; без согласованного расширения или без секции
    apps у сервера — вопрос агенту, который раскроет промпт для модели.
    """

    UNREAD: ClassVar[str] = "its prompts were not read yet"

    def __init__(
        self,
        registration: McpRegistration,
        servers: McpServers,
        relay: McpRelay,
    ) -> None:
        self._registration = registration
        self._servers = servers
        self._relay = relay
        self._names = McpToolNames()
        self._lines = CommandLines()
        self._apps = AppMeta()
        self._read = ServerPrompts(prompts=(), hints={}, feature=None, listens=False)
        self._unavailable: str | None = self.UNREAD
        """Почему сервер сейчас не даёт команд; None — сервер ответил."""
        self._following: asyncio.Task[None] | None = None

    @property
    def server(self) -> str:
        return self._registration.server

    @property
    def following(self) -> bool:
        """Поток смены промптов сервера открыт."""
        following = self._following
        if following is None:
            return False

        return not following.done()

    def commands(self) -> Sequence[MenuCommand]:
        if self._unavailable is not None:
            return ()

        return list(self._listed())

    def _listed(self) -> Iterator[MenuCommand]:
        for prompt in self._read.prompts:
            name = self._names.full(self.server, prompt.name)
            description = prompt.description
            if not description:
                description = prompt.name

            yield MenuCommand(
                name=name,
                description=description,
                hint=self._read.hints[name],
                icon=CommandIcon.MCP,
            )

    async def read(self) -> None:
        """Промпты сервера заново; сервер не ответил — его команды уходят
        из меню, причина — в лог и в отказ вызова."""
        try:
            read = await self._fetched()
        except (McpClientError, MCPError, AuthenticationError) as exc:
            self._unavailable = str(exc)
            logger.warning(
                "commands: mcp server %s gives no commands to %s: %s",
                self.server,
                self._registration.login,
                exc,
            )
            return

        self._read = read
        self._unavailable = None

    def follow(self, changed: Callable[[], Awaitable[None]]) -> None:
        """Поток смены промптов, если сервер её обещал и поток ещё не открыт;
        после каждой смены и конца потока — changed."""
        if self.following:
            return

        if not self._read.listens:
            return

        self._following = asyncio.create_task(
            self._followed(changed), name=f"commands-{self.server}"
        )

    async def close(self) -> None:
        following = self._following
        self._following = None
        if following is None:
            return

        following.cancel()
        await asyncio.gather(following, return_exceptions=True)

    async def run(self, call: CommandCall, board: CommandBoard) -> str | None:
        """Вызов промпта сервера: приложение — во вкладку (None), иначе текст
        вопроса агенту.

        Ошибки:
        CommandError — сервер недоступен, аргументы не разложить или сервер
            отказал в вызове инструмента.
        """
        if self._unavailable is not None:
            msg = (
                f"The command /{call.name} did not run: the MCP server "
                f"{self.server!r} is unavailable ({self._unavailable}). Try "
                "again when the server is back."
            )
            raise CommandError(msg)

        prompt = self._prompt(call.name)
        if prompt is None:
            return call.text()

        marked = self._marked(prompt)
        if marked is None:
            return call.text()

        window = await self._opened(call, prompt, marked)
        await board.app_opened(window)

        return None

    def _prompt(self, name: str) -> mt.Prompt | None:
        for prompt in self._read.prompts:
            if self._names.full(self.server, prompt.name) == name:
                return prompt

        return None

    def _marked(self, prompt: mt.Prompt) -> PromptCommand | None:
        """Пометка промпта, которую вкладка исполняет сама: сервер объявил
        расширение команд и чат рисует его приложения; иначе промпт —
        обычный, для модели. Пометка не по модели — обычный промпт, причина в
        логе."""
        feature = self._read.feature
        if feature is None:
            return None

        if self._servers.apps(self.server) is None:
            return None

        try:
            marked = feature.command(prompt)
        except ValidationError as exc:
            logger.warning(
                "commands: mcp server %s marks prompt %s off the %s mark, the "
                "prompt goes to the model: %s",
                self.server,
                prompt.name,
                CommandsFeature.ID,
                ValidationText.of(exc),
            )
            return None

        if marked is None:
            return None

        if marked.action is not CommandAction.APP:
            return None

        return marked

    async def _opened(
        self, call: CommandCall, prompt: mt.Prompt, marked: PromptCommand
    ) -> AppWindow:
        """tools/call инструмента пометки от имени пользователя вкладки и
        приложение этого инструмента.

        Ошибки:
        CommandError — аргументы не разложить, сервер отказал или у
            инструмента нет приложения.
        """
        try:
            arguments = self._lines.arguments(call, self._declared(prompt))
        except CommandArgumentsError as exc:
            raise CommandError(f"{exc}.") from exc

        try:
            view = await self._view(call, marked)
        except MCPError as exc:
            msg = (
                f"The command /{call.name} did not open its app: the MCP server "
                f"{self.server!r} refused to list its tools: {exc.error.message}"
            )
            raise CommandError(msg) from exc
        except (McpClientError, AuthenticationError) as exc:
            msg = (
                f"The command /{call.name} did not open its app: the tools of the "
                f"MCP server {self.server!r} were not read: {exc}"
            )
            raise CommandError(msg) from exc

        params = mt.CallToolRequestParams(name=marked.tool, arguments=arguments)
        try:
            result = await self._relay.app_call(self._registration, params)
        except MCPError as exc:
            msg = (
                f"The command /{call.name} did not open its app: the MCP server "
                f"{self.server!r} refused tools/call of {marked.tool!r}: "
                f"{exc.error.message}"
            )
            raise CommandError(msg) from exc
        except (McpClientError, AuthenticationError) as exc:
            msg = (
                f"The command /{call.name} did not open its app: tools/call of "
                f"{marked.tool!r} on the MCP server {self.server!r} failed: {exc}"
            )
            raise CommandError(msg) from exc

        dumped = result.model_dump(mode="json", by_alias=True, exclude_none=True)

        return AppWindow(
            view=view, call_id=uuid4().hex, arguments=arguments, result=dumped
        )

    async def _view(self, call: CommandCall, marked: PromptCommand) -> ToolAppView:
        """Приложение инструмента пометки по его карточке.

        Ошибки:
        CommandError — у сервера нет такого инструмента или у инструмента
            нет приложения.
        McpClientError, MCPError, AuthenticationError — список инструментов
            не прочитан.
        """
        port = await self._port()
        listed = await port.tools(McpServers.OWN)

        card: mt.Tool | None = None
        for tool in listed.tools:
            if tool.name == marked.tool:
                card = tool

        if card is None:
            msg = (
                f"The command /{call.name} did not open its app: the MCP server "
                f"{self.server!r} names the tool {marked.tool!r}, which it does "
                "not list."
            )
            raise CommandError(msg)

        try:
            config = self._apps.of(card)
        except ValidationError as exc:
            msg = (
                f"The command /{call.name} did not open its app: the tool "
                f"{marked.tool!r} of the MCP server {self.server!r} carries "
                f"_meta.ui off the MCP Apps form: {ValidationText.of(exc)}"
            )
            raise CommandError(msg) from exc

        resource = None
        if config is not None:
            resource = config.resource_uri

        if resource is None:
            msg = (
                f"The command /{call.name} did not open its app: the tool "
                f"{marked.tool!r} of the MCP server {self.server!r} has no "
                "_meta.ui.resourceUri."
            )
            raise CommandError(msg)

        apps = self._servers.apps(self.server)
        if apps is None:
            msg = (
                f"The command /{call.name} did not open its app: the chat draws "
                f"no apps of the MCP server {self.server!r}."
            )
            raise CommandError(msg)

        return ToolAppView(
            server=self.server,
            tool=card,
            resource_uri=resource,
            sandbox_url=str(apps.sandbox_url),
        )

    async def _followed(self, changed: Callable[[], Awaitable[None]]) -> None:
        """Поток смены промптов: подтверждённый поток и каждая смена —
        список заново; конец потока (сервер ушёл или закрыл поток) — тоже,
        и сервер, который не ответил, уходит из меню."""
        notifications = mt.SubscriptionFilter(prompts_list_changed=True)
        try:
            port = await self._port()
            async with port.listen(McpServers.OWN, notifications) as subscription:
                # смену до подтверждения поток не несёт: список — заново
                await self.read()
                await changed()
                async for event in subscription:
                    if not isinstance(event, PromptsListChanged):
                        continue

                    await self.read()
                    await changed()
        except (McpClientError, MCPError, AuthenticationError) as exc:
            logger.warning(
                "commands: the prompt changes stream of mcp server %s ended: %s",
                self.server,
                exc,
            )

        await self.read()
        await changed()

    async def _fetched(self) -> ServerPrompts:
        """Ошибки:
        McpClientError — сервер недоступен.
        MCPError — сервер ответил ошибкой MCP.
        AuthenticationError — вход пользователя вкладки больше не годен.
        """
        port = await self._port()
        described = await port.described(McpServers.OWN)
        capabilities = described.result.capabilities

        prompts: list[mt.Prompt] = []
        if capabilities.prompts is not None:
            prompts = await self._pages(port)

        hints: dict[str, str] = {}
        for prompt in prompts:
            name = self._names.full(self.server, prompt.name)
            hints[name] = await self._hint(port, capabilities, prompt)

        listens = False
        if capabilities.prompts is not None:
            listens = bool(capabilities.prompts.list_changed)

        return ServerPrompts(
            prompts=prompts,
            hints=hints,
            feature=described.commands(),
            listens=listens,
        )

    async def _pages(self, port: McpToolServer) -> list[mt.Prompt]:
        prompts: list[mt.Prompt] = []
        params: mt.PaginatedRequestParams | None = None
        while True:
            page = await port.list_prompts(McpServers.OWN, params)
            prompts.extend(page.prompts)
            if page.next_cursor is None:
                return prompts

            params = mt.PaginatedRequestParams(cursor=page.next_cursor)

    async def _hint(
        self,
        port: McpToolServer,
        capabilities: mt.ServerCapabilities,
        prompt: mt.Prompt,
    ) -> str:
        """Аргументы промпта по порядку, у аргумента с подсказками сервера —
        их значения через «|»."""
        parts: list[str] = []
        for argument in self._declared(prompt):
            values: Sequence[str] = ()
            if capabilities.completions is not None:
                values = await self._values(port, prompt, argument.name)

            if not values:
                parts.append(argument.name)
                continue

            listed = "|".join(values)
            parts.append(f"{argument.name} ({listed})")

        return " ".join(parts)

    async def _values(
        self, port: McpToolServer, prompt: mt.Prompt, argument: str
    ) -> Sequence[str]:
        """Значения аргумента, которые подсказывает сервер; отказ сервера
        ошибкой MCP — подсказка без значений, причина в логе."""
        params = mt.CompleteRequestParams(
            ref=mt.PromptReference(name=prompt.name),
            argument=mt.CompletionArgument(name=argument, value=""),
        )
        try:
            completed = await port.complete(McpServers.OWN, params)
        except MCPError as exc:
            logger.warning(
                "commands: mcp server %s gave no values for argument %s of prompt "
                "%s: %s",
                self.server,
                argument,
                prompt.name,
                exc.error.message,
            )
            return ()

        return completed.completion.values

    async def _port(self) -> McpToolServer:
        """Порт сервера от имени пользователя вкладки.

        Ошибки:
        AuthenticationError — вход пользователя вкладки больше не годен.
        McpClientError — сервера нет в [mcp.servers].
        """
        subject = self._registration.context().subject
        caller = McpCaller(login=subject.login, roles=subject.roles)

        return await self._servers.port(self.server, caller)

    @staticmethod
    def _declared(prompt: mt.Prompt) -> Sequence[CommandArgument]:
        declared = prompt.arguments
        if declared is None:
            return ()

        arguments: list[CommandArgument] = []
        for argument in declared:
            required = bool(argument.required)
            arguments.append(CommandArgument(name=argument.name, required=required))

        return arguments


class HarnessCommand(StrEnum):
    """Команды самого чата: их имена зарезервированы, одноимённая команда
    агента в меню не попадает."""

    CONNECTIONS = "connections"


class HarnessCommands(CommandSource):
    """Команды самого чата: реестр CARDS и обработчики; исполняет их вкладка,
    агент о них не знает. connections — соединения, выданные пользователю
    вкладки, таблицей во вкладке: тот же каталог хранилища соединений чата
    (GrantedConnections), что модель читает инструментом connection_list.
    Создаёт фабрика CommandMenus на реестр вкладки."""

    CARDS: ClassVar[Mapping[HarnessCommand, str]] = {
        HarnessCommand.CONNECTIONS: "Show the connections granted to you",
    }
    TITLE: ClassVar[str] = "Your connections"

    def __init__(self, registration: McpRegistration, store: StoreRef) -> None:
        self._registration = registration
        self._connections = GrantedConnections(store)

    def commands(self) -> Sequence[MenuCommand]:
        listed: list[MenuCommand] = []
        for command, description in self.CARDS.items():
            listed.append(
                MenuCommand(
                    name=command.value,
                    description=description,
                    hint="",
                    icon=CommandIcon.HARNESS,
                )
            )

        return listed

    def owns(self, name: str) -> bool:
        return name in self.CARDS

    async def run(self, call: CommandCall, board: CommandBoard) -> None:
        """Ошибки:
        CommandError — вход не годен или соединения не прочитаны.
        """
        try:
            subject = self._registration.context().subject
        except AuthenticationError as exc:
            msg = f"The command /{call.name} did not run: {exc.message}"
            raise CommandError(msg) from exc

        try:
            table = await self._connections.rows(subject)
        except ConnectionStoreError as exc:
            msg = (
                f"The command /{call.name} did not run: your connections were "
                f"not read: {exc}"
            )
            raise CommandError(msg) from exc

        await board.table_shown(self.TITLE, table)


class CommandMenu:
    """Реестр команд одной вкладки треда: меню поля ввода из команд чата,
    агента и серверов MCP профиля и маршрут вызова к владельцу.

    Создаёт его агент треда (ThreadAgent) фабрикой CommandMenus и держит,
    пока жива вкладка. Меню уходит вкладке (CommandBoard.offered) при любой
    смене источника. Имена команд чата зарезервированы: одноимённая
    команда агента в меню не попадает, причина — в логе; команды серверов
    всегда с приставкой своего сервера и ни с чем не сталкиваются. Вызов —
    выбранная в меню команда (command сообщения chainlit, текст — её
    аргументы) либо набранный текст `/<имя> <аргументы>`.
    """

    def __init__(
        self,
        board: CommandBoard,
        harness: HarnessCommands,
        servers: Sequence[McpCommands],
    ) -> None:
        self._board = board
        self._harness = harness
        self._agent = AgentCommands()
        self._servers = servers
        self._lines = CommandLines()
        self._names = McpToolNames()
        self._offered: list[MenuCommand] | None = None
        self._reading: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Меню — во вкладку сразу с командами чата; серверы читаются и
        открывают потоки смены промптов отдельной задачей, их команды меню
        получает по мере ответа."""
        await self._offer()
        self._read_later(self._servers)

    async def heard(self, update: schema.AvailableCommandsUpdate) -> None:
        """Агент объявил свои команды заново."""
        self._agent.heard(update)
        await self._offer()

    def revisit(self) -> None:
        """Серверы без открытого потока смены промптов перечитываются
        отдельной задачей: вернувшийся сервер возвращает свои команды в
        меню."""
        stale: list[McpCommands] = []
        for server in self._servers:
            if not server.following:
                stale.append(server)

        self._read_later(stale)

    async def routed(self, step_id: str, text: str, command: str | None) -> str | None:
        """Вопрос вкладки step_id с текстом text и выбранной в меню командой
        command: текст вопроса агенту либо None — команду исполнила вкладка,
        шаг вопроса снят.

        Ошибки:
        CommandError — команду сервера или чата не исполнить.
        """
        call = self._call(text, command)
        if call is None:
            return text

        if self._harness.owns(call.name):
            await self._harness.run(call, self._board)
            await self._board.taken(step_id)
            return None

        server = self._server_of(call.name)
        if server is None:
            return call.text()

        asked = await server.run(call, self._board)
        if asked is not None:
            return asked

        await self._board.taken(step_id)

        return None

    async def close(self) -> None:
        reading = self._reading
        self._reading = None
        if reading is not None:
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

        for server in self._servers:
            await server.close()

    def _call(self, text: str, command: str | None) -> CommandCall | None:
        if command:
            return CommandCall(name=command, rest=text.strip())

        return self._lines.parsed(text)

    def _server_of(self, name: str) -> McpCommands | None:
        parsed = self._names.parse(name)
        if parsed is None:
            return None

        for server in self._servers:
            if server.server == parsed.server:
                return server

        return None

    def _read_later(self, servers: Sequence[McpCommands]) -> None:
        """Серверы servers читаются отдельной задачей; задача уже идёт или
        читать некого — ничего."""
        reading = self._reading
        if reading is not None and not reading.done():
            return

        if not servers:
            return

        self._reading = asyncio.create_task(self._read(servers), name="commands-read")

    async def _read(self, servers: Sequence[McpCommands]) -> None:
        reading: list[Awaitable[None]] = []
        for server in servers:
            reading.append(server.read())

        await asyncio.gather(*reading)
        for server in servers:
            server.follow(self._offer)

        await self._offer()

    async def _offer(self) -> None:
        """Меню вкладке, если оно сменилось."""
        merged = list(self._merged())
        if merged == self._offered:
            return

        self._offered = merged
        await self._board.offered(merged)

    def _merged(self) -> Iterator[MenuCommand]:
        yield from self._harness.commands()
        for command in self._agent.commands():
            if self._harness.owns(command.name):
                logger.warning(
                    "commands: the agent command /%s is left out of the menu, the "
                    "chat reserves this name for its own command",
                    command.name,
                )
                continue

            yield command

        for server in self._servers:
            yield from server.commands()


class CommandMenus:
    """Фабрика реестров команд вкладок: на вкладку — команды чата и по
    источнику на каждый сервер MCP профиля, от имени её пользователя.
    Создаётся провайдером процесса из серверов MCP, ретранслятора и
    хранилища соединений чата; реестр создаёт агент треда."""

    def __init__(self, servers: McpServers, relay: McpRelay, store: StoreRef) -> None:
        self._servers = servers
        self._relay = relay
        self._store = store

    def menu(self, registration: McpRegistration, board: CommandBoard) -> CommandMenu:
        """Реестр вкладки регистрации registration (пользователь, тред,
        серверы профиля) над вкладкой board."""
        sources: list[McpCommands] = []
        for server in sorted(registration.servers):
            own = replace(registration, server=server)
            sources.append(McpCommands(own, self._servers, self._relay))

        harness = HarnessCommands(registration, self._store)

        return CommandMenu(board, harness, sources)
