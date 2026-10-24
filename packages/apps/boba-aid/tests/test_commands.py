"""Команды «/» вкладки настоящим путём: агент треда (`boba --stdio` с фейковой
моделью), серверы MCP профиля — обычный сервер на SDK mcp (standard_server.py)
по HTTP с расширением com.boba/commands и по stdio без него — и реестр команд
вкладки, который рисует меню и окна в приёмник теста.

Проверяется: меню собирается из команд чата, агента (ACP
`available_commands_update`) и промптов серверов под полными именами и
следует за сменой промптов; вызов каждого вида уходит владельцу — команда
агента и промпт сервера без расширения агенту вопросом, помеченный промпт
сервера с расширением — tools/call и приложение во вкладке, команда чата —
таблица во вкладке; имя чата зарезервировано; недоступный сервер уходит из
меню и отказывает понятно, вернувшийся — возвращается; окна команд не
доходят ни до модели, ни до истории агента.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from secrets import token_bytes
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
import pytest
from acp.experimental.v2 import schema
from acp_stand import (
    API_KEY,
    AgentHistoryStand,
    AgentStand,
    FakeModel,
    ModelRequests,
    RecordingUpdates,
)
from aid_stand import RelayStand, TabSession

from boba.aid.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    Question,
    ThreadAgent,
    ThreadIdentity,
)
from boba.aid.agent.commands import CommandError, CommandIcon, MenuCommand
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chat.profiles import ChatProfileConfig, SelectedProfile
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.secrets import SecretCipher
from boba.connections.stored import GrantTarget, StoredRole
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    HttpEndpoint,
    McpCaller,
    McpServerApps,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpServersConfig,
)
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.stand.names import StandNames
from boba.stand.signin import SignInStand
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import REPO_ROOT, StandPorts
from boba.transport.http import HttpTransportConfig

pytestmark = pytest.mark.anyio

SCHEMA = StandNames().of_tree("commands")
CONNECTIONS_SCHEMA = StandNames().of_tree("commands_conn")
EXTENDED = "cmds"
"""Сервер с расширением com.boba/commands и секцией apps."""
PLAIN = "plain"
"""Тот же сервер без расширения, тоже с секцией apps."""
CONSOLE = "orders_console"
"""Промпт сервера с пометкой «открыть приложение» и его инструмент."""
ORDERS_APP = "ui://standard/orders.html"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


class HttpStandard:
    """Стандартный сервер (standard_server.py) по HTTP с флагами flags на
    своём постоянном порту: его можно остановить и поднять снова там же."""

    SCRIPT: ClassVar[Path] = (
        REPO_ROOT / "packages/services/boba-mcp-client/tests/standard_server.py"
    )
    HOST: ClassVar[str] = "127.0.0.1"
    BOOT_SEC: ClassVar[float] = 60.0
    STOP_SEC: ClassVar[float] = 10.0

    def __init__(self, flags: Sequence[str]) -> None:
        self.port = StandPorts().free()
        self._flags = list(flags)
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        """Ошибки:
        AssertionError — сервер не начал слушать за BOOT_SEC.
        """
        args = [sys.executable, str(self.SCRIPT), "streamable-http", str(self.port)]
        self._process = subprocess.Popen([*args, *self._flags])
        deadline = time.monotonic() + self.BOOT_SEC
        while True:
            try:
                with socket.create_connection((self.HOST, self.port), 1.0):
                    return
            except OSError as exc:
                if time.monotonic() > deadline:
                    msg = (
                        f"standard server {args} is not listening on port "
                        f"{self.port} within {self.BOOT_SEC}s: {exc}"
                    )
                    raise AssertionError(msg) from exc

                time.sleep(0.2)

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        process.terminate()
        try:
            process.wait(timeout=self.STOP_SEC)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.STOP_SEC)

    def config(self, sandbox: str) -> McpServerConfig:
        """Запись сервера с секцией apps и песочницей sandbox."""
        endpoint = HttpEndpoint.model_validate(
            {
                "transport": "streamable-http",
                "service": {
                    "scheme": "http",
                    "host": self.HOST,
                    "port": self.port,
                    "path": "",
                },
                "path": "/mcp",
            }
        )

        return McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(connect_timeout_sec=20.0, call_timeout_sec=60.0),
            apps=McpServerApps.model_validate({"sandbox_url": sandbox}),
        )


@pytest.fixture
def extended() -> Iterator[HttpStandard]:
    server = HttpStandard(["--commands"])
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
async def users(test_postgres: PostgresConfig, pool: AsyncPostgresPool) -> UsersTable:
    return UsersTable(test_postgres, SCHEMA, pool)


@pytest.fixture
async def threads(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> ThreadsTable:
    """Таблица тредов в схеме теста: в threads.meta агент вкладки помнит сессию."""
    runtime_schema = files("boba.runtime") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(conn, runtime_schema, {})

    return ThreadsTable(test_postgres, SCHEMA)


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    """Соединения чата в схеме теста; роль dev вкладки известна хранилищу."""
    schema_dir = files("boba.connection_broker") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(CONNECTIONS_SCHEMA).apply_files(conn, schema_dir, {})

    cipher = SecretCipher(token_bytes(32))
    built = ConnectionStore(
        pool, CONNECTIONS_SCHEMA, cipher, ConnectionTypes.discover()
    )
    await built.sync_roles(list(CommandsTab.ROLES))

    return built


class CommandsTab:
    """Вкладка теста: агент треда над серверами EXTENDED и PLAIN профиля,
    его реестр команд рисует меню и окна в updates; tab — вход вкладки."""

    ROLES: ClassVar[frozenset[str]] = frozenset({"dev"})

    def __init__(  # noqa: PLR0913 — вкладка собирается из фикстур теста
        self,
        root: Path,
        stand: Stand,
        users: UsersTable,
        threads: ThreadsTable,
        model: FakeModel,
        servers: McpServersConfig,
        store: ConnectionStore,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(lambda: store, lambda: credentials, CallContexts())
        self.servers = McpServers(servers)
        history = AgentHistoryStand(stand)
        agent_stand = AgentStand(root / "agents", history.history())
        launcher = AgentLauncher(
            agent_stand.config(),
            servers,
            SignInStand.profiles(),
            users,
            relays.registrations,
            relays.relay(self.servers),
            "0.0",
        )
        tab = TabSession()
        tab.password_sign_in(self.ROLES)
        self.identity = ThreadIdentity(
            thread_id=str(uuid4()),
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=self.ROLES,
            session=tab.live,
            profile=SelectedProfile(
                name=SignInStand.PROFILE, config=self._profile(model)
            ),
        )
        self._launcher = launcher
        self._meta = AgentSessionMeta(threads)
        self._menus = relays.menus(self.servers)
        self.menus = self._menus
        self.updates = RecordingUpdates()
        self.agent = self.opened(self.updates)

    def opened(self, updates: RecordingUpdates) -> ThreadAgent:
        """Агент того же треда в вкладке, которая рисует в updates."""
        return ThreadAgent(
            self.identity, self._launcher, self._meta, updates, self._menus
        )

    async def turn(self, text: str) -> None:
        await self.agent.ask(Question(key=uuid4().hex, text=text))

    async def call(self, server: str, tool: str) -> None:
        """Вызов инструмента сервера мимо агента: стенд меняет сервер."""
        caller = McpCaller(login=TabSession.LOGIN, roles=self.ROLES)
        port = await self.servers.port(server, caller)
        params = mt.CallToolRequestParams(name=tool, arguments={})
        await port.call(McpServers.OWN, params, None, None, None, None)

    async def close(self) -> None:
        await self.agent.close()
        await self.servers.stop()

    @staticmethod
    def _profile(model: FakeModel) -> ChatProfileConfig:
        provider = {
            "kind": "openai",
            "connection": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": model.port,
                "path": "/v1",
                "auth": {"method": "bearer", "token": API_KEY},
            },
            "transport": HttpTransportConfig().model_dump(mode="json"),
        }
        base = SignInStand.profile().model_dump(mode="json")

        return ChatProfileConfig.model_validate(
            {
                **base,
                "provider": provider,
                "model": "fake-model",
                "mcp": [EXTENDED, PLAIN],
            }
        )


@pytest.fixture
async def tab(  # noqa: PLR0913 — вкладка собирается из фикстур теста
    tmp_path: Path,
    stand: Stand,
    users: UsersTable,
    threads: ThreadsTable,
    model: FakeModel,
    standard: McpServerConfig,
    extended: HttpStandard,
    store: ConnectionStore,
    auth_token: str,
) -> AsyncIterator[CommandsTab]:
    """Вкладка с запущенным агентом; auth_token — секрет входа стенда: им
    подписан вход вкладки."""
    plain = standard.model_copy(
        update={
            "apps": McpServerApps.model_validate(
                {"sandbox_url": f"http://localhost:{StandPorts().free()}/sandbox.html"}
            )
        }
    )
    sandbox = f"http://localhost:{StandPorts().free()}/sandbox.html"
    servers = McpServersConfig(
        servers={EXTENDED: extended.config(sandbox), PLAIN: plain}
    )
    opened = CommandsTab(tmp_path, stand, users, threads, model, servers, store)
    try:
        await opened.agent.start()
        yield opened
    finally:
        await opened.close()


def has(*names: str) -> Any:
    """Меню, где есть все names."""

    def wanted(menu: Sequence[str]) -> bool:
        for name in names:  # noqa: SIM110
            if name not in menu:
                return False

        return True

    return wanted


def lacks(prefix: str) -> Any:
    """Меню без команд с началом prefix (или с этим именем)."""

    def wanted(menu: Sequence[str]) -> bool:
        for name in menu:  # noqa: SIM110
            if name.startswith(prefix):
                return False

        return True

    return wanted


def last_menu(updates: RecordingUpdates) -> dict[str, MenuCommand]:
    commands: dict[str, MenuCommand] = {}
    for command in updates.menus[-1]:
        commands[command.name] = command

    return commands


class TestMenu:
    """Меню вкладки из трёх источников и его обновление по уведомлениям."""

    async def test_menu_gathers_the_chat_the_agent_and_the_servers(
        self, tab: CommandsTab
    ) -> None:
        """Сразу — команда чата; по ответу серверов — их промпты под полными
        именами с подсказками аргументов (значения — completion/complete);
        с открытием сессии — команды агента по ACP."""
        greet = f"mcp__{EXTENDED}__greet"
        console = f"mcp__{PLAIN}__{CONSOLE}"

        first = RecordingUpdates.names(tab.updates.menus[0])
        await tab.updates.menu(has("connections", greet, console), 0)
        await tab.turn(ScenarioName.ANSWER.value)
        names = await tab.updates.menu(has("compact", greet), 0)
        menu = last_menu(tab.updates)

        assert first == ["connections"], first
        assert names[0] == "connections", names
        assert menu["connections"].icon is CommandIcon.HARNESS
        assert menu["compact"].icon is CommandIcon.AGENT
        assert menu["compact"].hint == "optional instructions for the summary"
        assert menu[greet].icon is CommandIcon.MCP
        assert menu[greet].hint == "name style (plain|poem|pirate)", menu[greet]
        assert menu[console].hint == "status", menu[console]

    async def test_menu_follows_the_prompt_changes_of_a_server(
        self, tab: CommandsTab
    ) -> None:
        """notifications/prompts/list_changed потоком subscriptions/listen:
        новый промпт сервера появляется в меню без хода, убранный — уходит."""
        extra = f"mcp__{EXTENDED}__extra_prompt"
        await tab.updates.menu(has(f"mcp__{EXTENDED}__greet"), 0)

        added = len(tab.updates.menus)
        await tab.call(EXTENDED, "toggle_prompt")
        await tab.updates.menu(has(extra), added)
        removed = len(tab.updates.menus)
        await tab.call(EXTENDED, "toggle_prompt")
        names = await tab.updates.menu(lacks(extra), removed)

        assert f"mcp__{EXTENDED}__greet" in names, names

    async def test_agent_command_named_as_a_chat_command_is_left_out(
        self, tab: CommandsTab
    ) -> None:
        """Имя чата зарезервировано: одноимённая команда агента в меню не
        попадает, команда чата остаётся одна."""
        updates = RecordingUpdates()
        registration = replace(tab.identity.registration(PLAIN), servers=frozenset())
        menu = tab.menus.menu(registration, updates)
        clash = schema.AvailableCommand(name="connections", description="agent's")
        own = schema.AvailableCommand(name="review", description="review the diff")
        await menu.start()

        await menu.heard(
            schema.AvailableCommandsUpdate(available_commands=[clash, own])
        )
        await menu.close()

        listed = updates.menus[-1]
        assert RecordingUpdates.names(listed) == ["connections", "review"]
        assert listed[0].icon is CommandIcon.HARNESS


class TestRoutes:
    """Вызов каждого вида уходит владельцу команды."""

    async def test_agent_command_goes_to_the_agent_as_a_question(
        self, tab: CommandsTab
    ) -> None:
        """Выбранная в меню команда агента — вопрос `/<имя> <аргументы>`;
        набранный текст — как есть."""
        bare = await tab.agent.routed(Question(key="q1", text=""), "compact")
        given = await tab.agent.routed(
            Question(key="q2", text="keep the sql"), "compact"
        )
        typed = await tab.agent.routed(Question(key="q3", text="/compact now"), None)
        plain = await tab.agent.routed(Question(key="q4", text="hello"), None)

        assert bare is not None
        assert given is not None
        assert typed is not None
        assert plain is not None
        assert bare.text == "/compact", bare
        assert given.text == "/compact keep the sql", given
        assert typed.text == "/compact now", typed
        assert plain.text == "hello", plain
        assert tab.updates.taken_steps == []

    async def test_marked_prompt_of_a_server_with_the_extension_opens_its_app(
        self, tab: CommandsTab, model: FakeModel
    ) -> None:
        """Сервер и чат объявили com.boba/commands, промпт помечен: вкладка
        сама зовёт tools/call инструмента пометки и показывает его
        приложение; модель не участвует, шаг вопроса снят."""
        name = f"mcp__{EXTENDED}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)
        asked = len(model.app.requests)

        chosen = await tab.agent.routed(Question(key="q1", text="new"), name)
        typed = await tab.agent.routed(
            Question(key="q2", text=f"/{name} shipped"), None
        )

        assert chosen is None
        assert typed is None
        assert tab.updates.taken_steps == ["q1", "q2"]
        assert len(model.app.requests) == asked
        first, second = tab.updates.windows
        assert first.view.server == EXTENDED
        assert first.view.tool.name == CONSOLE
        assert first.view.resource_uri == ORDERS_APP
        assert first.arguments == {"status": "new"}
        assert second.arguments == {"status": "shipped"}
        result = mt.CallToolResult.model_validate(first.result)
        shown = result.content[0]
        assert isinstance(shown, mt.TextContent)
        assert shown.text == "the orders console is open for new orders"

    async def test_marked_prompt_of_a_server_without_the_extension_goes_to_the_model(
        self, tab: CommandsTab, model: FakeModel
    ) -> None:
        """Сервер расширения не объявил: пометку промпта вкладка не замечает,
        команда — вопрос агенту, агент раскрывает промпт для модели."""
        name = f"mcp__{PLAIN}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)
        status = f"new {ScenarioName.ANSWER.value}"

        routed = await tab.agent.routed(Question(key="q1", text=status), name)
        assert routed is not None
        await tab.agent.ask(routed)

        assert routed.text == f"/{name} {status}"
        assert tab.updates.windows == []
        assert ModelRequests(model).saw(
            "Open the orders console for the orders with status new"
        ), ModelRequests(model).read()

    async def test_chat_command_shows_its_table_in_the_tab(
        self,
        tab: CommandsTab,
        model: FakeModel,
        store: ConnectionStore,
        test_postgres: PostgresConfig,
    ) -> None:
        """connections — соединения, выданные пользователю, таблицей во
        вкладке тем же инструментом чата, что видит модель; агент и модель
        не участвуют."""
        name = f"orders-{uuid4().hex[:8]}"
        roles = StoredRole.by_name(await store.roles())
        connection_id = await store.add(name, test_postgres)
        await store.grant(connection_id, GrantTarget.role(roles["dev"]))
        asked = len(model.app.requests)

        routed = await tab.agent.routed(Question(key="q1", text=""), "connections")

        assert routed is None
        assert tab.updates.taken_steps == ["q1"]
        assert len(model.app.requests) == asked
        title, table = tab.updates.tables[-1]
        names: list[str] = []
        for row in table.rows:
            names.append(str(row["name"]))

        assert title == "Your connections"
        assert name in names, table

    async def test_wrong_arguments_of_an_app_command_are_refused(
        self, tab: CommandsTab
    ) -> None:
        """Обязательный аргумент промпта не назван — отказ с причиной, окна
        нет."""
        name = f"mcp__{EXTENDED}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)

        with pytest.raises(CommandError, match="argument 'status' is required"):
            await tab.agent.routed(Question(key="q1", text=""), name)

        assert tab.updates.windows == []


class TestUnavailableServer:
    async def test_server_that_left_takes_its_commands_and_brings_them_back(
        self, tab: CommandsTab, extended: HttpStandard
    ) -> None:
        """Сервер ушёл: поток смены промптов оборвался, перечитывание не
        удалось — его команды уходят из меню, вызов отказывает понятно.
        Сервер вернулся: конец следующего хода перечитывает его, команды
        снова в меню."""
        greet = f"mcp__{EXTENDED}__greet"
        await tab.updates.menu(has(greet), 0)

        left = len(tab.updates.menus)
        await asyncio.to_thread(extended.stop)
        await tab.updates.menu(lacks(f"mcp__{EXTENDED}__"), left)
        with pytest.raises(CommandError, match=f"MCP server '{EXTENDED}' is unavail"):
            await tab.agent.routed(Question(key="q1", text="new"), greet)

        back = len(tab.updates.menus)
        await asyncio.to_thread(extended.start)
        await tab.turn(ScenarioName.ANSWER.value)
        names = await tab.updates.menu(has(greet), back)

        assert f"mcp__{PLAIN}__greet" in names, names


class TestHistory:
    async def test_windows_of_commands_stay_out_of_the_agent_history(
        self, tab: CommandsTab, model: FakeModel
    ) -> None:
        """Окна команд чата и приложений сервера модель не видела — в
        истории агента их нет: воспроизведение треда другой вкладке несёт
        только настоящий ход."""
        name = f"mcp__{EXTENDED}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)
        await tab.agent.routed(Question(key="q1", text=""), "connections")
        await tab.agent.routed(Question(key="q2", text="new"), name)
        await tab.turn(f"hello {ScenarioName.ANSWER.value}")

        replayed = RecordingUpdates()
        other = tab.opened(replayed)
        try:
            await other.start()
            loaded = await other.resume()
        finally:
            await other.close()

        texts: list[str] = []
        for _, update in replayed.updates:
            if not isinstance(update, schema.UserMessageUpdate):
                continue

            for block in update.content or ():
                if isinstance(block, schema.TextContentBlock):
                    texts.append(block.text)

        assert loaded
        assert texts == [f"hello {ScenarioName.ANSWER.value}"], texts
        seen = ModelRequests(model)
        assert not seen.saw("the orders console is open"), seen.read()
        assert not seen.saw("Your connections"), seen.read()
