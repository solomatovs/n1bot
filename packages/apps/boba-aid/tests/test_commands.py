"""Команды «/» вкладки настоящим путём: агент треда (`boba --stdio` с фейковой
моделью), серверы MCP профиля — обычный сервер на SDK mcp (standard_server.py)
по HTTP с расширением com.boba/commands и по stdio без него — и реестр команд
вкладки, который рисует меню и окна в приёмник теста.

Проверяется: меню вкладки — только команды сессии из истории агента
(`_boba/commands`): команда чата (объявлена агенту `_boba/session/record`),
команды агента и промпты и интерфейсы серверов под полными именами, с
подсказками аргументов; смена промптов сервера и его обрыв доходят до меню
записью истории; вызов каждого вида уходит владельцу — команда агента и
промпт сервера без расширения агенту вопросом, интерфейс сервера с
расширением — tools/call и приложение целиком (карточка, итог, ресурс),
команда чата — таблица; исполненная вкладкой команда и её отказ — запись
истории агента, её эхо приходит вкладке вживую и при воспроизведении, модель
её не видит; запись посреди хода ложится на границе шага; агент без
расширения — меню из стандартного списка, всё уходит ему вопросом; сервер,
переименованный посреди чата, — в меню и запросе модели только под новым
именем, через записи истории.
"""

from __future__ import annotations

import ast
import asyncio
import json
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from enum import StrEnum
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
from boba.aid.agent.commands import CommandIcon, MenuCommand
from boba.aid.agent.relay import ChatTools
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
from boba.transport.acp.protocol import (
    BobaExtension,
    CommandApp,
    CommandFailure,
    CommandOutcome,
    CommandOutcomeUpdate,
    CommandText,
)
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

    def config(self, sandbox: int) -> McpServerConfig:
        """Запись сервера с секцией apps и песочницей на порту sandbox."""
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
            apps=McpServerApps.model_validate(
                {"chat_url": "http://127.0.0.1:8122/aid/", "port": sandbox}
            ),
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
    """Вкладка теста: агент треда thread_id над серверами профиля servers,
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
        thread_id: str,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        relays = RelayStand(lambda: store, lambda: credentials, CallContexts())
        self.servers = McpServers(servers)
        self.history = AgentHistoryStand(stand)
        agent_stand = AgentStand(root / "agents", self.history.history())
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
            thread_id=thread_id,
            user_id=uuid4(),
            login=TabSession.LOGIN,
            roles=self.ROLES,
            session=tab.live,
            profile=SelectedProfile(
                name=SignInStand.PROFILE,
                config=self._profile(model, sorted(servers.servers)),
            ),
        )
        self._launcher = launcher
        self.meta = AgentSessionMeta(threads)
        self._menus = relays.menus(self.servers)
        self.menus = self._menus
        self.updates = RecordingUpdates()
        self.agent = self.opened(self.updates)

    def opened(self, updates: RecordingUpdates) -> ThreadAgent:
        """Агент того же треда в вкладке, которая рисует в updates."""
        return ThreadAgent(
            self.identity, self._launcher, self.meta, updates, self._menus
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
    def _profile(model: FakeModel, servers: Sequence[str]) -> ChatProfileConfig:
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
                "mcp": list(servers),
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
                {"chat_url": "http://127.0.0.1:8122/aid/", "port": 8124}
            )
        }
    )
    sandbox = 8123
    servers = McpServersConfig(
        servers={EXTENDED: extended.config(sandbox), PLAIN: plain}
    )
    opened = CommandsTab(
        tmp_path, stand, users, threads, model, servers, store, str(uuid4())
    )
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


def user_lines(updates: RecordingUpdates) -> list[tuple[str, str]]:
    """Строки вопросов ленты по порядку: (messageId, текст)."""
    lines: list[tuple[str, str]] = []
    for _, update in updates.updates:
        if not isinstance(update, schema.UserMessageUpdate):
            continue

        texts: list[str] = []
        for block in update.content or ():
            if isinstance(block, schema.TextContentBlock):
                texts.append(block.text)

        lines.append((update.message_id, "".join(texts)))

    return lines


def outcomes(updates: RecordingUpdates) -> list[CommandOutcomeUpdate]:
    """Итоги команд вкладки, которые агент отдал лентой, по порядку."""
    found: list[CommandOutcomeUpdate] = []
    for _, update in updates.updates:
        if not isinstance(update, schema.OtherSessionUpdate):
            continue

        if update.session_update != BobaExtension.COMMAND_OUTCOME.value:
            continue

        dumped = update.model_dump(mode="json", by_alias=True)
        found.append(CommandOutcomeUpdate.model_validate(dumped))

    return found


def outcome_of(updates: RecordingUpdates, command: str) -> CommandOutcome:
    """Итог последней записи команды command; запись — за строкой вызова с
    тем же messageId."""
    found = outcomes(updates)
    lines = dict(user_lines(updates))
    for update in reversed(found):
        if update.command != command:
            continue

        assert update.message_id in lines, (update, lines)
        return update.outcome

    raise AssertionError(f"no outcome of /{command} among {found}")


def last_menu(updates: RecordingUpdates) -> dict[str, MenuCommand]:
    commands: dict[str, MenuCommand] = {}
    for command in updates.menus[-1]:
        commands[command.name] = command

    return commands


class TestMenu:
    """Меню вкладки — команды сессии из истории агента."""

    async def test_menu_is_the_commands_of_the_session_history(
        self, tab: CommandsTab
    ) -> None:
        """Новая вкладка без вопросов: команда чата, команда агента и
        промпты и интерфейсы серверов под полными именами с подсказками
        аргументов (значения — completion/complete) — и всё это записями
        commands_delta в истории сессии, которые меню и повторяет."""
        greet = f"mcp__{EXTENDED}__greet"
        console = f"mcp__{PLAIN}__{CONSOLE}"
        app = f"mcp__{EXTENDED}__{CONSOLE}"

        names = await tab.updates.menu(
            has("connections", "compact", greet, console, app), 0
        )
        menu = last_menu(tab.updates)
        session_id = tab.agent.session_id
        assert session_id is not None
        recorded = await self._recorded(tab, session_id, len(names))

        assert menu["connections"].icon is CommandIcon.HARNESS
        assert menu["compact"].icon is CommandIcon.AGENT
        assert menu["compact"].hint == "instructions"
        assert menu[greet].icon is CommandIcon.MCP
        assert menu[greet].hint == "name style (plain|poem|pirate)", menu[greet]
        assert menu[console].hint == "status", menu[console]
        assert menu[app].icon is CommandIcon.MCP
        declared: list[str] = []
        kinds: dict[str, str] = {}
        for record in recorded:
            for entry in record["added"]:
                declared.append(entry["name"])
                kinds[entry["name"]] = entry["type"]

        assert sorted(set(declared)) == sorted(names), (declared, names)
        assert kinds["connections"] == "harness"
        assert kinds["compact"] == "agent"
        assert kinds[greet] == "prompt"
        assert kinds[console] == "prompt"
        assert kinds[app] == "app"

    @staticmethod
    async def _recorded(
        tab: CommandsTab, session_id: str, count: int
    ) -> list[dict[str, Any]]:
        """Записи commands_delta сессии, когда в журнале их команд не меньше
        count: писатель истории сбрасывает записи сам по себе."""
        async with asyncio.timeout(30.0):
            while True:
                recorded = await tab.history.records(session_id, "commands-delta")
                named: set[str] = set()
                for record in recorded:
                    for entry in record["added"]:
                        named.add(entry["name"])

                if len(named) >= count:
                    return recorded

                await asyncio.sleep(0.2)

    async def test_tab_without_a_question_leaves_no_session(
        self, tab: CommandsTab
    ) -> None:
        """Сессия, которую вкладка открыла ради команд, тред не узнаёт, пока
        нет вопроса, а закрытие вкладки удаляет её у агента."""
        names = await tab.updates.menu(has("compact", "connections"), 0)
        opened = tab.agent.session_id
        assert opened is not None

        recorded = await tab.meta.session_of(tab.identity.thread_id)
        await self._recorded(tab, opened, len(names))
        held = await tab.history.rows(opened)
        await tab.agent.close()
        left = await tab.history.rows(opened)

        assert recorded is None, recorded
        assert held > 0
        assert left == 0

    async def test_first_question_keeps_the_session_of_the_tab(
        self, tab: CommandsTab
    ) -> None:
        """Первый вопрос идёт в сессию, открытую вкладкой, и пишет её в
        threads.meta; закрытие вкладки её не трогает."""
        await tab.updates.menu(has("compact"), 0)
        opened = tab.agent.session_id
        assert opened is not None

        await tab.turn(ScenarioName.ANSWER.value)
        recorded = await tab.meta.session_of(tab.identity.thread_id)
        await tab.agent.close()
        left = await tab.history.rows(opened)

        assert recorded == opened, recorded
        assert left > 0

    async def test_menu_follows_the_prompt_changes_of_a_server(
        self, tab: CommandsTab
    ) -> None:
        """notifications/prompts/list_changed потоком subscriptions/listen
        агента: новый промпт сервера — запись истории и строка меню без хода,
        убранный — уходит так же."""
        extra = f"mcp__{EXTENDED}__extra_prompt"
        await tab.updates.menu(has(f"mcp__{EXTENDED}__greet"), 0)

        added = len(tab.updates.menus)
        await tab.call(EXTENDED, "toggle_prompt")
        await tab.updates.menu(has(extra), added)
        removed = len(tab.updates.menus)
        await tab.call(EXTENDED, "toggle_prompt")
        names = await tab.updates.menu(lacks(extra), removed)

        assert f"mcp__{EXTENDED}__greet" in names, names

    async def test_agent_without_the_extension_lists_only_its_own_commands(
        self, tab: CommandsTab
    ) -> None:
        """Агент не ведёт команды сессии в истории: меню — его стандартный
        список, команд, которые исполняет вкладка, нет, и любой вызов уходит
        агенту вопросом; объявлять команды чата некуда."""
        updates = RecordingUpdates()
        registration = tab.identity.registration(ChatTools.NAME)
        menu = tab.menus.menu(registration, updates)
        greet = f"mcp__{EXTENDED}__greet"
        listed = schema.AvailableCommandsUpdate(
            available_commands=[
                schema.AvailableCommand(name="compact", description="compact"),
                schema.AvailableCommand(name=greet, description="greet"),
            ]
        )
        await menu.start(False)
        await menu.listed(listed)

        asked = await menu.routed("plain", greet)
        chat = await menu.routed("", "connections")

        assert RecordingUpdates.names(updates.menus[-1]) == ["compact", greet]
        assert asked == f"/{greet} plain", asked
        assert chat == "/connections", chat
        assert menu.declaration("s-1") is None


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
        assert outcomes(tab.updates) == []

    async def test_marked_prompt_of_a_server_with_the_extension_opens_its_app(
        self, tab: CommandsTab, model: FakeModel
    ) -> None:
        """Сервер и чат объявили com.boba/commands, промпт помечен: вкладка
        сама зовёт tools/call инструмента пометки и читает ресурс его
        приложения; приложение целиком — запись истории агента, её эхо —
        строка вызова и итог; модель не участвует, шаг вопроса уступил эху."""
        name = f"mcp__{EXTENDED}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)
        asked = len(model.app.requests)

        chosen = await tab.agent.routed(Question(key="q1", text="new"), name)
        typed = await tab.agent.routed(
            Question(key="q2", text=f"/{name} shipped"), None
        )

        assert chosen is None
        assert typed is None
        assert tab.updates.inserted_questions == ["q1", "q2"]
        assert len(model.app.requests) == asked
        lines = [text for _, text in user_lines(tab.updates)]
        assert lines == [f"/{name} new", f"/{name} shipped"], lines
        first, second = outcomes(tab.updates)
        assert isinstance(first.outcome, CommandApp)
        assert isinstance(second.outcome, CommandApp)
        app = first.outcome
        assert app.server == EXTENDED
        assert app.tool.name == CONSOLE
        assert app.arguments == {"status": "new"}
        assert second.outcome.arguments == {"status": "shipped"}
        shown = app.result.content[0]
        assert isinstance(shown, mt.TextContent)
        assert shown.text == "the orders console is open for new orders"
        page = app.resource.contents[0]
        assert isinstance(page, mt.TextResourceContents)
        assert str(page.uri) == ORDERS_APP
        assert page.mime_type == "text/html;profile=mcp-app"
        assert "<html" in page.text.lower(), page.text[:200]

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
        assert outcomes(tab.updates) == []
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
        """connections — соединения, выданные пользователю, таблицей тем же
        каталогом чата, что видит модель, записью истории агента; модель
        не участвует."""
        name = f"orders-{uuid4().hex[:8]}"
        roles = StoredRole.by_name(await store.roles())
        connection_id = await store.add(name, test_postgres)
        await store.grant(connection_id, GrantTarget.role(roles["dev"]))
        asked = len(model.app.requests)

        routed = await tab.agent.routed(Question(key="q1", text=""), "connections")

        assert routed is None
        assert tab.updates.inserted_questions == ["q1"]
        assert len(model.app.requests) == asked
        shown = outcome_of(tab.updates, "connections")
        assert isinstance(shown, CommandText)
        assert shown.text.startswith("**Your connections**"), shown
        assert name in shown.text, shown

    async def test_wrong_arguments_of_an_app_command_are_refused(
        self, tab: CommandsTab
    ) -> None:
        """Обязательный аргумент промпта не назван — отказ с причиной
        записью истории вместо приложения."""
        name = f"mcp__{EXTENDED}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)

        routed = await tab.agent.routed(Question(key="q1", text=""), name)

        assert routed is None
        refused = outcome_of(tab.updates, name)
        assert isinstance(refused, CommandFailure), refused
        assert "argument 'status' is required" in refused.error, refused

    async def test_command_during_a_turn_joins_it_at_the_step_boundary(
        self, tab: CommandsTab
    ) -> None:
        """Команда посреди вызова инструмента ложится в историю после его
        итога; ход вкладки кончается одним idle и не ждёт лишнего."""
        slow = {"name": f"mcp__{PLAIN}__sleep_for", "arguments": {"seconds": 1.5}}
        question = f"{ScenarioName.CALL.value} {json.dumps(slow)}"
        turn = asyncio.create_task(tab.turn(question))
        await self.started_call(tab.updates)

        routed = await tab.agent.routed(Question(key="q9", text=""), "connections")
        await turn

        assert routed is None
        assert len(tab.updates.finished) == 1, tab.updates.finished
        order: list[str] = []
        for _, update in tab.updates.updates:
            if isinstance(update, schema.SessionToolCallUpdate):
                if update.status == "completed":
                    order.append("tool")

            if not isinstance(update, schema.OtherSessionUpdate):
                continue

            if update.session_update == BobaExtension.COMMAND_OUTCOME.value:
                order.append("command")

        assert order.count("command") == 1, order
        assert order[-1] == "command", order
        assert order[0] == "tool", order

    @staticmethod
    async def started_call(updates: RecordingUpdates) -> None:
        """Ждёт начала вызова инструмента в ходе."""
        async with asyncio.timeout(60.0):
            while True:
                for _, update in updates.updates:
                    if isinstance(update, schema.SessionToolCallUpdate):
                        return

                await asyncio.sleep(0.05)


class TestUnavailableServer:
    async def test_server_that_left_takes_its_commands_and_brings_them_back(
        self, tab: CommandsTab, extended: HttpStandard
    ) -> None:
        """Сервер ушёл: поток смены списков агента оборвался, перечитывание
        не удалось — запись истории снимает его команды, и они уходят из
        меню. Сервер вернулся: начало следующего хода перечитывает его,
        запись истории возвращает команды в меню."""
        greet = f"mcp__{EXTENDED}__greet"
        await tab.updates.menu(has(greet), 0)

        left = len(tab.updates.menus)
        await asyncio.to_thread(extended.stop)
        await tab.updates.menu(lacks(f"mcp__{EXTENDED}__"), left)

        back = len(tab.updates.menus)
        await asyncio.to_thread(extended.start)
        await tab.turn(ScenarioName.ANSWER.value)
        names = await tab.updates.menu(has(greet), back)

        assert f"mcp__{PLAIN}__greet" in names, names


class TestHistory:
    async def test_commands_of_the_tab_replay_as_they_were_shown(
        self, tab: CommandsTab, model: FakeModel, extended: HttpStandard
    ) -> None:
        """Команды чата и приложения сервера — в истории агента: другая
        вкладка получает воспроизведением те же строки вызовов и те же итоги,
        что пришли вживую, — приложение целиком, даже когда сервера уже нет;
        модель их не видела."""
        name = f"mcp__{EXTENDED}__{CONSOLE}"
        await tab.updates.menu(has(name), 0)
        await tab.agent.routed(Question(key="q1", text=""), "connections")
        await tab.agent.routed(Question(key="q2", text="new"), name)
        await tab.turn(f"hello {ScenarioName.ANSWER.value}")
        live_lines = [text for _, text in user_lines(tab.updates)]
        live = outcomes(tab.updates)
        await asyncio.to_thread(extended.stop)

        replayed = RecordingUpdates()
        other = tab.opened(replayed)
        try:
            await other.start()
            loaded = await other.resume()
        finally:
            await other.close()

        assert loaded
        lines = [text for _, text in user_lines(replayed)]
        assert lines == live_lines, lines
        assert lines == [
            "/connections",
            f"/{name} new",
            f"hello {ScenarioName.ANSWER.value}",
        ], lines
        assert outcomes(replayed) == live
        seen = ModelRequests(model)
        assert not seen.saw("the orders console is open"), seen.read()
        assert not seen.saw("Your connections"), seen.read()


class TestRenamedServer:
    OLD: ClassVar[str] = "boba_general"
    NEW: ClassVar[str] = "aid_all"

    async def test_server_renamed_mid_chat_is_only_its_new_name(  # noqa: PLR0913 — вкладки собираются из фикстур
        self,
        tmp_path: Path,
        stand: Stand,
        users: UsersTable,
        threads: ThreadsTable,
        model: FakeModel,
        standard: McpServerConfig,
        extended: HttpStandard,
        store: ConnectionStore,
    ) -> None:
        """Сервер профиля переименован между вкладками одного треда: вторая
        вкладка открывает ту же сессию, и при подключении агент пишет одну
        запись команд — снятые старые имена и новые команды; меню и запрос
        модели знают только новое имя, а беседа до переименования на месте."""
        sandbox = StandPorts().free()
        thread_id = str(uuid4())
        first = CommandsTab(
            tmp_path / "old",
            stand,
            users,
            threads,
            model,
            McpServersConfig(servers={self.OLD: extended.config(sandbox)}),
            store,
            thread_id,
        )
        try:
            await first.agent.start()
            await first.updates.menu(has(f"mcp__{self.OLD}__greet"), 0)
            await first.turn(f"before {ScenarioName.ANSWER.value}")
            session_id = first.agent.session_id
        finally:
            await first.close()

        assert session_id is not None
        second = CommandsTab(
            tmp_path / "new",
            stand,
            users,
            threads,
            model,
            McpServersConfig(servers={self.NEW: extended.config(sandbox)}),
            store,
            thread_id,
        )
        try:
            await second.agent.start()
            loaded = await second.agent.resume()
            names = await second.updates.menu(has(f"mcp__{self.NEW}__greet"), 0)
            await second.turn(f"after {ScenarioName.ANSWER.value}")
        finally:
            await second.close()

        assert loaded
        assert not [name for name in names if self.OLD in name], names
        recorded = await second.history.records(session_id, "commands-delta")
        renamed = recorded[-1]
        assert f"mcp__{self.OLD}__greet" in renamed["removed"], renamed
        added: list[str] = []
        for entry in renamed["added"]:
            added.append(entry["name"])
            assert entry["owner"] in (self.NEW, "agent", "harness"), entry

        assert f"mcp__{self.NEW}__greet" in added, added
        tools: list[str] = []
        for tool in ModelRequests(model).last().get("tools", ()):
            tools.append(tool["function"]["name"])

        assert not [name for name in tools if self.OLD in name], tools
        assert ModelRequests(model).saw(f"before {ScenarioName.ANSWER.value}")


class HistoryOnly(StrEnum):
    """Конструкторы меню и объявления команд, которые живут только в одном
    месте: строки меню и маршруты вкладки — в реестре команд вкладки,
    объявления команд клиенту — в формах команд агента над историей."""

    MENU = "MenuCommand"
    AGENT_ROUTE = "AgentRoute"
    APP_ROUTE = "AppRoute"
    HARNESS_ROUTE = "HarnessRoute"
    AVAILABLE = "AvailableCommandsUpdate"
    DECLARED = "CommandsUpdate"


class McpListing(StrEnum):
    """Запросы описаний сервера MCP, которых реестр команд вкладки не шлёт:
    команды он берёт только из объявления агента."""

    LIST_PROMPTS = "list_prompts"
    GET_PROMPT = "get_prompt"
    LISTEN = "listen"
    DESCRIBED = "described"
    TOOLS = "tools"
    COMPLETE = "complete"
    PORT = "port"


class TestOnlyFromHistory:
    """Меню и маршрут команд обойти нельзя: строки меню и маршруты строит
    только реестр команд вкладки из объявления агента, объявления команд
    клиенту строят только формы команд агента над историей сессии, а реестр
    вкладки не спрашивает описаний у серверов MCP."""

    PACKAGES: ClassVar[Path] = Path(__file__).resolve().parents[3]
    MENU_MODULE: ClassVar[Path] = (
        PACKAGES / "apps/boba-aid/src/boba/aid/agent/commands.py"
    )
    AGENT_MODULE: ClassVar[Path] = PACKAGES / "apps/boba-cli/src/boba/cli/acp_server.py"
    FORMS: ClassVar[str] = "CommandForms"

    def test_menu_and_routes_are_built_only_by_the_menu_of_the_tab(self) -> None:
        for name in (
            HistoryOnly.MENU,
            HistoryOnly.AGENT_ROUTE,
            HistoryOnly.APP_ROUTE,
            HistoryOnly.HARNESS_ROUTE,
        ):
            assert set(self._makers(name.value)) == {self.MENU_MODULE}, name

    def test_commands_reach_the_client_only_from_the_session_history(self) -> None:
        for name in (HistoryOnly.AVAILABLE, HistoryOnly.DECLARED):
            assert set(self._makers(name.value)) == {self.AGENT_MODULE}, name
            classes = set(self._owners(self.AGENT_MODULE, name.value))
            assert classes == {self.FORMS}, (name, classes)

    def test_menu_of_the_tab_asks_no_mcp_server_for_its_commands(self) -> None:
        tree = ast.parse(self.MENU_MODULE.read_text(encoding="utf-8"))
        asked: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if not isinstance(func, ast.Attribute):
                continue

            asked.add(func.attr)

        listing = {member.value for member in McpListing}
        assert not asked & listing, sorted(asked & listing)

    def _makers(self, name: str) -> Iterator[Path]:
        """Исходники пакетов (без тестов), где зовётся конструктор name."""
        for path in self.PACKAGES.rglob("*.py"):
            if "tests" in path.parts:
                continue

            source = path.read_text(encoding="utf-8")
            if f"{name}(" not in source:
                continue

            if any(self._calls(ast.parse(source), name)):
                yield path.resolve()

    def _owners(self, path: Path, name: str) -> Iterator[str]:
        """Классы модуля path, в которых зовётся конструктор name."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue

            if any(self._calls(node, name)):
                yield node.name

    @staticmethod
    def _calls(tree: ast.AST, name: str) -> Iterator[bool]:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if isinstance(func, ast.Name) and func.id == name:
                yield True

            if isinstance(func, ast.Attribute) and func.attr == name:
                yield True
