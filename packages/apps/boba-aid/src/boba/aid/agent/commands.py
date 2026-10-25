"""Команды «/» вкладки чата: меню поля ввода и маршрут вызова к владельцу
команды — только из команд сессии, которые агент ведёт в своей истории.

Агент сверяет с историей команды серверов MCP (при каждом подключении,
переподключении и обрыве), свои команды и объявление чата и шлёт все
команды сессии целиком уведомлением расширения `_boba/commands`
(CommandsUpdate): вид, владелец, аргументы, у интерфейса MCP Apps —
инструмент и приложение. Единственный вход меню и маршрута —
CommandMenu.heard: никакой другой источник (prompts/list серверов, реестр
чата) в меню не попадает. Свои команды чат объявляет агенту записью
`_boba/session/record` вида commands (CommandMenu.declaration) и получает
их обратно из истории. Маршрут — по виду команды (CommandRoutes): команда
чата — её обработчик (HarnessCommands); интерфейс MCP Apps — tools/call
инструмента от имени пользователя и его приложение целиком (AppRoute);
промпт сервера и команда агента — вопрос агенту `/<имя> <аргументы>`.
Исполненная вкладкой команда — итог (ExecutedCommand), который агент треда
пишет в историю агента: модель его не видит, лента рисует его вживую и при
воспроизведении одинаково. Агент без расширения команд сессии — меню из
стандартного `available_commands_update` (CommandMenu.listed), все команды
уходят агенту вопросом; команд, которые исполняет вкладка, нет.

Ошибки:
CommandError — команду не исполнить: аргументы не разложить, сервер
    недоступен, сервер или чат отказали в вызове.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar, Protocol

import mcp_types as mt
from acp.experimental.v2 import schema
from mcp.shared.exceptions import MCPError

from boba.aid.agent.relay import McpRegistration, McpRelay
from boba.connection_broker.tools import GrantedConnections
from boba.connection_broker.user_connections import StoreRef
from boba.connections.stored import ConnectionStoreError
from boba.core.commands import (
    CommandArgument,
    CommandArgumentsError,
    CommandCall,
    CommandLines,
)
from boba.identity.errors import AuthenticationError
from boba.mcp_client.client import McpClientError, McpServers
from boba.toolkit.result import MarkdownTable, NoteLine, TableResult
from boba.transport.acp.protocol import (
    CommandApp,
    CommandFailure,
    CommandOutcome,
    CommandsUpdate,
    CommandText,
    DeclaredAgentCommand,
    DeclaredApp,
    DeclaredArgument,
    DeclaredCommand,
    DeclaredHarnessCommand,
    DeclaredPrompt,
    SessionCommandsRequest,
)

__all__ = [
    "AgentRoute",
    "AppRoute",
    "CommandBoard",
    "CommandError",
    "CommandIcon",
    "CommandMenu",
    "CommandMenus",
    "CommandRoute",
    "CommandRoutes",
    "ExecutedCommand",
    "HarnessCommand",
    "HarnessCommands",
    "HarnessRoute",
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

    @property
    def rank(self) -> int:
        """Место владельца в меню: команды чата, затем агента, затем
        серверов."""
        match self:
            case CommandIcon.HARNESS:
                return 0
            case CommandIcon.AGENT:
                return 1
            case CommandIcon.MCP:
                return 2


@dataclass(frozen=True)
class MenuCommand:
    """Команда меню поля ввода: имя без косой черты, что она делает,
    подсказка аргументов и значок владельца."""

    name: str
    description: str
    hint: str
    icon: CommandIcon


@dataclass(frozen=True)
class ExecutedCommand:
    """Команда, которую исполнила вкладка: вызов и итог для истории агента.
    Строит реестр вкладки CommandMenu, в историю пишет агент треда."""

    call: CommandCall
    outcome: CommandOutcome


class CommandBoard(Protocol):
    """Вкладка глазами реестра команд: меню поля ввода. Реализует приёмник
    потока вкладки AgentFeed (feed.py)."""

    @abstractmethod
    async def offered(self, commands: Sequence[MenuCommand]) -> None:
        """Меню поля ввода вкладки — эти команды, полным списком."""


class CommandRoute(Protocol):
    """Маршрут одной команды сессии: её строка в меню и исполнение вызова.
    Реализации — по виду команды (AgentRoute, AppRoute, HarnessRoute);
    строит их только фабрика CommandRoutes из объявления агента."""

    @abstractmethod
    def command(self) -> MenuCommand:
        """Строка команды в меню."""

    @abstractmethod
    async def run(self, call: CommandCall) -> str | CommandOutcome:
        """Текст вопроса агенту либо итог команды, которую исполнила вкладка.

        Ошибки:
        CommandError — команду, которую исполняет вкладка, не исполнить.
        """


class AgentRoute(CommandRoute):
    """Реализация CommandRoute для команды, которую исполняет агент: промпт
    сервера (агент раскроет его для модели) или команда сессии агента.
    Вызов — вопрос агенту текстом `/<имя> <аргументы>`."""

    def __init__(self, command: MenuCommand) -> None:
        self._command = command

    def command(self) -> MenuCommand:
        return self._command

    async def run(self, call: CommandCall) -> str | CommandOutcome:
        return call.text()


class HarnessCommand(StrEnum):
    """Команды самого чата: их имена объявляет чат агенту."""

    CONNECTIONS = "connections"


class HarnessCommands:
    """Команды самого чата: объявление агенту (CARDS) и обработчики; в меню
    они приходят из истории агента. connections — соединения, выданные
    пользователю вкладки, таблицей во вкладке: тот же каталог хранилища
    соединений чата (GrantedConnections), что модель читает инструментом
    connection_list. Итог — markdown, каким его увидел пользователь. Создаёт
    фабрика CommandMenus на реестр вкладки."""

    CARDS: ClassVar[Sequence[DeclaredHarnessCommand]] = (
        DeclaredHarnessCommand(
            name=HarnessCommand.CONNECTIONS.value,
            description="Show the connections granted to you",
        ),
    )
    TITLE: ClassVar[str] = "Your connections"

    def __init__(self, registration: McpRegistration, store: StoreRef) -> None:
        self._registration = registration
        self._connections = GrantedConnections(store)

    def declaration(self, session_id: str) -> SessionCommandsRequest:
        """Объявление команд чата агенту сессии session_id."""
        return SessionCommandsRequest(session_id=session_id, commands=list(self.CARDS))

    async def run(self, call: CommandCall) -> CommandText:
        """Ошибки:
        CommandError — команды такой у чата нет, вход не годен или
            соединения не прочитаны.
        """
        if call.name != HarnessCommand.CONNECTIONS.value:
            msg = (
                f"The command /{call.name} did not run: the agent names it a command "
                "of this chat, and the chat has no such command."
            )
            raise CommandError(msg)

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

        return CommandText(text=self._markdown(table))

    def _markdown(self, table: TableResult) -> str:
        rows: list[dict[str, str]] = []
        for row in table.rows:
            cells: dict[str, str] = {}
            for key, value in row.items():
                cells[key] = self._cell(value)

            rows.append(cells)

        parts = [f"**{self.TITLE}**"]
        if rows:
            parts.append(MarkdownTable.render(rows))

        if table.note:
            parts.append(NoteLine.render(table.note))

        return "\n\n".join(parts)

    @staticmethod
    def _cell(value: object) -> str:
        if value is None:
            return ""

        return str(value)


class HarnessRoute(CommandRoute):
    """Реализация CommandRoute для команды чата: исполняет её обработчик
    чата (HarnessCommands)."""

    def __init__(self, command: MenuCommand, harness: HarnessCommands) -> None:
        self._command = command
        self._harness = harness

    def command(self) -> MenuCommand:
        return self._command

    async def run(self, call: CommandCall) -> str | CommandOutcome:
        return await self._harness.run(call)


class AppRoute(CommandRoute):
    """Реализация CommandRoute для интерфейса MCP Apps сервера: вкладка сама
    зовёт tools/call инструмента объявления от имени пользователя и читает
    ресурс его приложения; итог — приложение целиком (CommandApp) с
    карточкой инструмента из объявления. Модель не участвует."""

    def __init__(
        self,
        command: MenuCommand,
        declared: DeclaredApp,
        arguments: Sequence[CommandArgument],
        registration: McpRegistration,
        relay: McpRelay,
    ) -> None:
        self._command = command
        self._declared = declared
        self._arguments = arguments
        self._registration = registration
        self._relay = relay
        self._lines = CommandLines()

    def command(self) -> MenuCommand:
        return self._command

    async def run(self, call: CommandCall) -> str | CommandOutcome:
        """Ошибки:
        CommandError — аргументы не разложить, сервер отказал или недоступен.
        """
        server = self._declared.owner
        tool = self._declared.tool.name
        try:
            arguments = self._lines.arguments(call, self._arguments)
        except CommandArgumentsError as exc:
            raise CommandError(f"{exc}.") from exc

        params = mt.CallToolRequestParams(name=tool, arguments=arguments)
        try:
            result = await self._relay.app_call(self._registration, params)
        except MCPError as exc:
            msg = (
                f"The command /{call.name} did not open its app: the MCP server "
                f"{server!r} refused tools/call of {tool!r}: {exc.error.message}"
            )
            raise CommandError(msg) from exc
        except (McpClientError, AuthenticationError) as exc:
            msg = (
                f"The command /{call.name} did not open its app: tools/call of "
                f"{tool!r} on the MCP server {server!r} failed: {exc}"
            )
            raise CommandError(msg) from exc

        resource = await self._resource(call)

        return CommandApp(
            server=server,
            tool=self._declared.tool,
            arguments=dict(arguments),
            result=result,
            resource=resource,
        )

    async def _resource(self, call: CommandCall) -> mt.ReadResourceResult:
        """Ресурс приложения целиком (resources/read) от имени пользователя.

        Ошибки:
        CommandError — сервер отказал или недоступен.
        """
        server = self._declared.owner
        uri = self._declared.resource_uri
        params = mt.ReadResourceRequestParams(uri=uri)
        try:
            return await self._relay.app_read(self._registration, params)
        except MCPError as exc:
            msg = (
                f"The command /{call.name} did not open its app: the MCP server "
                f"{server!r} refused resources/read of {uri}: {exc.error.message}"
            )
            raise CommandError(msg) from exc
        except (McpClientError, AuthenticationError) as exc:
            msg = (
                f"The command /{call.name} did not open its app: resources/read "
                f"of {uri} on the MCP server {server!r} failed: {exc}"
            )
            raise CommandError(msg) from exc


class CommandRoutes:
    """Фабрика маршрутов команд вкладки: из объявления агента
    (`_boba/commands`) — маршрут по виду команды, из стандартного списка
    агента без расширения — вопрос агенту. Интерфейс сервера, приложения
    которого чат не рисует или которого нет у профиля треда, в меню не
    попадает (причина — в логе). Создаёт фабрика CommandMenus на вкладку."""

    def __init__(
        self,
        registration: McpRegistration,
        servers: McpServers,
        relay: McpRelay,
        harness: HarnessCommands,
    ) -> None:
        self._registration = registration
        self._servers = servers
        self._relay = relay
        self._harness = harness
        self._lines = CommandLines()

    @staticmethod
    def _arguments(declared: Sequence[DeclaredArgument]) -> Sequence[CommandArgument]:
        """Объявленные аргументы для разбора вызова и подсказки."""
        arguments: list[CommandArgument] = []
        for argument in declared:
            arguments.append(
                CommandArgument(
                    name=argument.name,
                    required=argument.required,
                    values=argument.values,
                )
            )

        return arguments

    def declared(self, commands: Sequence[DeclaredCommand]) -> Iterator[CommandRoute]:
        for declared in commands:
            route = self._route(declared)
            if route is None:
                continue

            yield route

    def listed(self, update: schema.AvailableCommandsUpdate) -> Iterator[CommandRoute]:
        for command in update.available_commands:
            hint = ""
            given = command.input
            if isinstance(given, schema.TextAvailableCommandInput):
                hint = given.hint

            yield AgentRoute(
                MenuCommand(
                    name=command.name,
                    description=command.description,
                    hint=hint,
                    icon=CommandIcon.AGENT,
                )
            )

    def _route(self, declared: DeclaredCommand) -> CommandRoute | None:
        arguments = self._arguments(declared.arguments)
        hint = self._lines.hint(arguments)
        match declared:
            case DeclaredPrompt():
                return AgentRoute(self._menu(declared, hint, CommandIcon.MCP))
            case DeclaredAgentCommand():
                return AgentRoute(self._menu(declared, hint, CommandIcon.AGENT))
            case DeclaredHarnessCommand():
                return HarnessRoute(
                    self._menu(declared, hint, CommandIcon.HARNESS), self._harness
                )
            case DeclaredApp():
                return self._app(declared, arguments, hint)

    def _app(
        self, declared: DeclaredApp, arguments: Sequence[CommandArgument], hint: str
    ) -> AppRoute | None:
        owner = declared.owner
        if owner not in self._registration.servers:
            logger.warning(
                "commands: /%s opens an app of the MCP server %s, which the profile "
                "of thread %s does not give; it is left out of the menu",
                declared.name,
                owner,
                self._registration.thread_id,
            )
            return None

        if self._servers.apps(owner) is None:
            logger.warning(
                "commands: /%s opens an app of the MCP server %s, and the chat draws "
                "no apps of it; it is left out of the menu",
                declared.name,
                owner,
            )
            return None

        return AppRoute(
            self._menu(declared, hint, CommandIcon.MCP),
            declared,
            arguments,
            replace(self._registration, server=owner),
            self._relay,
        )

    @staticmethod
    def _menu(declared: DeclaredCommand, hint: str, icon: CommandIcon) -> MenuCommand:
        return MenuCommand(
            name=declared.name,
            description=declared.description,
            hint=hint,
            icon=icon,
        )


class CommandMenu:
    """Реестр команд одной вкладки треда: меню поля ввода и маршрут вызова к
    владельцу — только из команд сессии, которые агент объявил из своей
    истории.

    Создаёт его агент треда (ThreadAgent) фабрикой CommandMenus и держит,
    пока жива вкладка. Агент с расширением команд сессии (start(True))
    объявляет их целиком (heard), стандартный список тогда не читается;
    агент без расширения — только стандартным списком (listed). Меню уходит
    вкладке (CommandBoard.offered) при каждой его смене. Вызов — выбранная
    в меню команда (command сообщения chainlit, текст — её аргументы) либо
    набранный текст `/<имя> <аргументы>`; команды, которой агент не
    объявлял, — вопрос агенту как есть. В меню команды чата идут первыми,
    затем агента, затем серверов в порядке истории. Отказ команды, которую
    исполняет вкладка, — тоже её итог для истории.
    """

    def __init__(
        self, board: CommandBoard, routes: CommandRoutes, harness: HarnessCommands
    ) -> None:
        self._board = board
        self._routes = routes
        self._harness = harness
        self._lines = CommandLines()
        self._known: Mapping[str, CommandRoute] = {}
        self._offered: list[MenuCommand] | None = None
        self._declared = False

    async def start(self, declared: bool) -> None:
        """Агент ведёт команды сессии в истории и объявляет их целиком
        (declared); меню — во вкладку сразу, пусть и пустое."""
        self._declared = declared
        await self._offer()

    def declaration(self, session_id: str) -> SessionCommandsRequest | None:
        """Объявление команд чата агенту открытой сессии; None — агент не
        ведёт команды сессии в истории."""
        if not self._declared:
            return None

        return self._harness.declaration(session_id)

    async def heard(self, update: CommandsUpdate) -> None:
        """Агент объявил команды сессии из истории целиком."""
        self._replace(self._routes.declared(update.commands))
        await self._offer()

    async def listed(self, update: schema.AvailableCommandsUpdate) -> None:
        """Стандартный список команд агента: меню из него, только если агент
        не объявляет команды целиком."""
        if self._declared:
            return

        self._replace(self._routes.listed(update))
        await self._offer()

    async def routed(self, text: str, command: str | None) -> str | ExecutedCommand:
        """Вопрос вкладки с текстом text и выбранной в меню командой command:
        текст вопроса агенту либо команда, которую исполнила вкладка, с её
        итогом — отказ тоже итог."""
        call = self._call(text, command)
        if call is None:
            return text

        route = self._known.get(call.name)
        if route is None:
            return call.text()

        try:
            done = await route.run(call)
        except CommandError as exc:
            return ExecutedCommand(call=call, outcome=CommandFailure(error=str(exc)))

        if isinstance(done, str):
            return done

        return ExecutedCommand(call=call, outcome=done)

    def _replace(self, routes: Iterator[CommandRoute]) -> None:
        known: dict[str, CommandRoute] = {}
        for route in routes:
            known[route.command().name] = route

        self._known = known

    def _call(self, text: str, command: str | None) -> CommandCall | None:
        if command:
            return CommandCall(name=command, rest=text.strip())

        return self._lines.parsed(text)

    @staticmethod
    def _rank(command: MenuCommand) -> int:
        return command.icon.rank

    async def _offer(self) -> None:
        """Меню вкладке, если оно сменилось."""
        menu: list[MenuCommand] = []
        for route in self._known.values():
            menu.append(route.command())

        menu.sort(key=self._rank)

        if menu == self._offered:
            return

        self._offered = menu
        await self._board.offered(menu)


class CommandMenus:
    """Фабрика реестров команд вкладок: на вкладку — команды чата и маршруты
    от имени её пользователя. Создаётся провайдером процесса из серверов
    MCP, ретранслятора и хранилища соединений чата; реестр создаёт агент
    треда."""

    def __init__(self, servers: McpServers, relay: McpRelay, store: StoreRef) -> None:
        self._servers = servers
        self._relay = relay
        self._store = store

    def menu(self, registration: McpRegistration, board: CommandBoard) -> CommandMenu:
        """Реестр вкладки регистрации registration (пользователь, тред,
        серверы профиля) над вкладкой board."""
        harness = HarnessCommands(registration, self._store)
        routes = CommandRoutes(registration, self._servers, self._relay, harness)

        return CommandMenu(board, routes, harness)
