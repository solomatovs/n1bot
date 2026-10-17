"""Чат исполняет инструменты своего агента: стенд chainlit, сервис boba-mcp
стенда с плагином pg процессами хоста, настоящий `boba acp` v2 с фейковой
моделью.

Агент получает серверы профиля и сервер chat объявлениями type "acp" и зовёт
их через mcp/message; ретранслятор чата запечатывает соединения и исполняет
вызовы от имени пользователя вкладки, инструменты chat — в процессе чата.
Проверяется ход «connection_list на сервере chat → pg_query на boba-mcp с
именем соединения»: имя подменяется запечатанным профилем, сервис открывает
его и исполняет запрос, в ленте остаётся имя; файл настроек агента не несёт
ни серверов, ни хуков, ни токенов.
Приёмка ретранслятора — настоящий агент и стенд boba-mcp со входом proxy:
инструмента чужой роли нет у агента, остановка отменяет вызов на сервере,
инструмент только для интерфейса агенту не виден и не вызывается."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
from acp.experimental.v2 import schema
from acp_stand import AgentHistoryStand, Agents, FakeModel, RecordingUpdates
from aid_stand import ServiceProcess, TabSession
from test_acp_chat import BOOT_TIMEOUT_SEC, AgentProcesses, ChatSocket

import boba.aid.agent
from boba.aid.agent.client import AgentProcess
from boba.aid.rendering.chat_view import StepStatus
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import McpServerConfig, McpServersConfig
from boba.runtime.config import RuntimeConfig
from boba.runtime.http import RequestTokens
from boba.stand.names import StandNames
from boba.stand.site import Stand
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import OfferedTools, ScenarioName, serve
from boba.stand.ui.stand import (
    ServiceStand,
    StandAgentAuth,
    StandApp,
    StandConfig,
    StandProcess,
    StandUrl,
    free_port,
)
from boba.toolkit.mcp import McpToolNames

pytestmark = pytest.mark.anyio

DB_NAME = "boba_acp_tools"
TOKEN_DELAY_SEC = 0.0
CONNECTION = "probe"
SERVICE_PLUGINS = ("pg",)
MCP_SERVER = "boba_general"
ADMIN = "admin"
SHORT_TOKEN_SEC = 4
"""Срок токена сервиса на стенде входа local: агент успевает его пережить."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Стенд живёт отдельным процессом: сессия chainlit в тесте не нужна."""


@pytest.fixture(scope="module")
def stand_database() -> StandDatabase:
    return StandDatabase(StandApp.AID, StandNames().of(DB_NAME))


@pytest.fixture(scope="module")
def db_name(stand_database: StandDatabase) -> str:
    return stand_database.prepare()


@pytest.fixture(scope="module")
def llm_port() -> int:
    return free_port()


class FakeLlmProcess:
    """FakeLlmApp отдельным процессом: модель агента на время модуля."""

    READY_SEC: ClassVar[float] = 30.0

    def __init__(self, port: int) -> None:
        self._port = port
        self._process = multiprocessing.Process(
            target=serve,
            args=(StandUrl.HOST.value, port, TOKEN_DELAY_SEC),
            daemon=True,
        )

    def start(self) -> None:
        self._process.start()
        url = StandUrl.of(self._port, "/v1/models")
        deadline = time.monotonic() + self.READY_SEC
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(url, timeout=1.0).status_code
                    < HTTPStatus.INTERNAL_SERVER_ERROR
                ):
                    return
            except httpx.HTTPError:
                time.sleep(0.2)

        msg = f"fake llm did not answer GET {url} in {self.READY_SEC}s"
        raise RuntimeError(msg)

    def stop(self) -> None:
        self._process.terminate()
        self._process.join(timeout=10)


@pytest.fixture(scope="module")
def fake_llm(llm_port: int) -> Iterator[None]:
    process = FakeLlmProcess(llm_port)
    process.start()
    try:
        yield
    finally:
        process.stop()


@pytest.fixture(scope="module")
def app(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    db_name: str,
) -> Iterator[StandProcess]:
    workdir = tmp_path_factory.mktemp("acp-tools-stand")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=db_name,
        service_plugins=SERVICE_PLUGINS,
    )
    process = StandProcess(config=config, log_path=workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def probe(
    app: StandProcess, stand_database: StandDatabase, stand: Stand
) -> PostgresConfig:
    """Соединение пользователя стенда: роль-пробник с паролем в своей базе,
    выдано всем ролям стенда после старта приложения (роли сеет оно)."""
    connection = AgentHistoryStand(stand).postgres()
    stand_database.add_stored_connection(CONNECTION, connection)

    return connection


@pytest.fixture(scope="module")
def login(app: StandProcess) -> httpx.Response:
    credential = app.config.credential(ADMIN)
    response = httpx.post(
        f"{app.config.base_url}/login",
        data={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    assert response.status_code == HTTPStatus.OK, response.text[:200]

    return response


@pytest.fixture(scope="module")
def cookie(login: httpx.Response) -> str:
    parts: list[str] = []
    for name, value in login.cookies.items():
        parts.append(f"{name}={value}")

    assert parts, "login returned no cookies"

    return "; ".join(parts)


@pytest.fixture(scope="module")
def session_jwt(login: httpx.Response, runtime_config: RuntimeConfig) -> str:
    """JWT входа пользователя из cookie: агенту он не передаётся, и входы
    чата для агента его не принимают."""
    token = RequestTokens(runtime_config.session.cookie).of_cookies(dict(login.cookies))
    assert token, "login cookies carry no sign-in token"

    return token


class AgentFiles:
    """Файл настроек процесса агента вкладки: то, что чат отдал агенту."""

    def __init__(self, app: StandProcess) -> None:
        self._agents = AgentProcesses(app)

    async def text(self, thread_id: str) -> str:
        pid = await self._agents.await_thread(thread_id, 0)
        path = self._agents.of_thread(thread_id)[pid]

        return path.read_text(encoding="utf-8")


@pytest.fixture
async def tab(
    app: StandProcess, cookie: str, probe: PostgresConfig
) -> AsyncIterator[ChatSocket]:
    socket = ChatSocket(app, cookie, str(uuid4()))
    await socket.open()
    try:
        yield socket
    finally:
        await socket.close()


class ToolSteps:
    """Шаги инструментов ленты по событиям сокета: имя шага — статусный кружок,
    имя инструмента и подпись вызова (ToolStepLabel), вход шага — markdown
    аргументов по виду вызова."""

    def __init__(self, socket: ChatSocket) -> None:
        self._socket = socket

    def named(self, tool: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event, payload in self._socket.events:
            if event not in ("new_message", "update_message"):
                continue

            if not isinstance(payload, dict):
                continue

            if payload.get("type") != "tool":
                continue

            if self._tool_of(str(payload.get("name", ""))).startswith(tool):
                found.append(payload)

        return found

    @staticmethod
    def _tool_of(name: str) -> str:
        """Имя инструмента без статусного кружка слева."""
        for status in StepStatus:
            prefix = status.title("")
            if name.startswith(prefix):
                return name[len(prefix) :]

        return name

    def last(self, tool: str) -> dict[str, Any]:
        steps = self.named(tool)
        assert steps, (tool, self._socket.trace())

        return steps[-1]


class TestAgentTurn:
    """Агент стенда зовёт connection_list на сервере chat и pg_query на boba-mcp."""

    async def test_connection_list_then_sealed_pg_query(
        self, app: StandProcess, tab: ChatSocket, probe: PostgresConfig
    ) -> None:
        companion = app.companion
        assert companion is not None
        since = app.log_lines()
        service_since = companion.log_lines()

        calls = [
            {"name": "mcp__chat__connection_list", "arguments": {}},
            {
                "name": "mcp__boba_general__pg_query",
                "arguments": {
                    "connection": CONNECTION,
                    "sql": "select 1 as one",
                    "offset": 0,
                    "limit": 10,
                },
            },
        ]
        await tab.send(f"{ScenarioName.CALL.value} {json.dumps(calls)}")
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))

        steps = ToolSteps(tab)
        listed = steps.last("connection_list")
        assert CONNECTION in str(listed.get("output")), (listed, app.tail(60))
        assert not listed.get("isError"), listed

        # вход шага — markdown аргументов: имя соединения как есть, не JWE
        queried = steps.last("pg_query")
        shown = str(queried.get("input"))
        assert CONNECTION in shown, queried
        assert "eyJ" not in shown, queried
        assert not queried.get("isError"), (queried, companion.tail(80))
        assert "one" in str(queried.get("output")), queried

        # запечатывает ретранслятор чата (CallSealing)
        app_log = _lines_since(app, since)
        assert any(
            f"sealing: connection {CONNECTION} of kind postgres sealed in connection "
            "of mcp__boba_general__pg_query" in line
            for line in app_log
        ), "\n".join(app_log[-40:])

        service_log = _lines_since(companion, service_since)
        assert any(
            "connection in connection (postgres)" in line for line in service_log
        ), "\n".join(service_log[-40:])

        assert not app.complaints(since), app.complaints(since)


class TestNestedCallsFeed:
    """Связка workflow сервера boba-mcp рисуется подшагами узлов; ход каждого
    узла приходит его подшагу по ключу узла в `_meta` уведомления сквозь
    сервер, ретранслятор чата и агента."""

    async def test_each_node_step_gets_its_own_progress(
        self, app: StandProcess, tab: ChatSocket, probe: PostgresConfig
    ) -> None:
        nodes = [
            {
                "key": "src",
                "tool": "pg_stream_out",
                "args": {
                    "intent": "rows out",
                    "connection": CONNECTION,
                    "sql": "select 1 as one",
                    "wire": "tsv",
                    "out": "rows",
                },
            },
            {
                "key": "drain",
                "tool": "dev_null",
                "args": {"intent": "drain rows", "feeds": ["rows"]},
            },
        ]
        calls = [{"name": "mcp__boba_general__workflow", "arguments": {"nodes": nodes}}]
        await tab.send(f"{ScenarioName.CALL.value} {json.dumps(calls)}")
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        steps = ToolSteps(tab)
        assert not steps.named("workflow"), tab.trace()

        source = steps.named("pg_stream_out")
        assert source, (tab.trace(), app.tail(60))
        assert " pg_stream_out · rows out" in str(source[-1].get("name")), source[-1]
        assert not source[-1].get("isError"), source[-1]

        outputs: list[str] = []
        for payload in source:
            outputs.append(str(payload.get("output")))

        assert any("connecting to" in output for output in outputs), outputs

        drained = steps.named("dev_null")
        assert drained, tab.trace()
        assert " dev_null · drain rows" in str(drained[-1].get("name")), drained[-1]
        for payload in drained:
            assert "connecting to" not in str(payload.get("output")), payload


def _lines_since(process: StandProcess[Any], since: int) -> list[str]:
    text = process.log_path.read_text(encoding="utf-8", errors="replace")

    return text.splitlines()[since:]


class TestNoTokenExchangeInTheChat:
    """Чат не получает токенов сервиса для агента и не кладёт их в файл."""

    async def test_agent_package_has_no_token_exchange(self) -> None:
        root = Path(boba.aid.agent.__path__[0])
        modules: list[str] = []
        sources: list[str] = []
        for path in sorted(root.glob("*.py")):
            modules.append(path.name)
            sources.append(path.read_text(encoding="utf-8"))

        assert "tokens.py" not in modules, modules
        for gone in ("McpAccessToken", "McpTokenError", "jwt-bearer", "token_endpoint"):
            assert gone not in "\n".join(sources), gone

    async def test_agent_gets_no_servers_hooks_or_tokens_by_file(
        self, app: StandProcess, tab: ChatSocket, session_jwt: str
    ) -> None:
        """Серверы агент получает объявлениями сессии, токенов ему не выдают:
        в файле настроек их нет, ход идёт без выдачи токенов."""
        since = app.log_lines()
        await tab.send(f"answer-{uuid4().hex} {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))

        text = await AgentFiles(app).text(tab.thread_id)
        document = json.loads(text)
        assert session_jwt not in text
        assert "mcpServers" not in document, document
        assert "hooks" not in document, document
        assert document["mcp"] is None, document

        log = "\n".join(_lines_since(app, since))
        assert "mcp token issued" not in log, log[-2000:]


@pytest.fixture(scope="module")
def local_app(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    db_name: str,
) -> Iterator[StandProcess]:
    """Второй стенд чата над той же базой: на сервис он и его агент входят
    учёткой агента (local), а токен сервиса живёт несколько секунд."""
    workdir = tmp_path_factory.mktemp("acp-tools-local")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=db_name,
        service_plugins=SERVICE_PLUGINS,
        agent_auth=StandAgentAuth.LOCAL,
        service_token_ttl_sec=SHORT_TOKEN_SEC,
    )
    process = StandProcess(config=config, log_path=workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
async def local_tab(
    local_app: StandProcess, probe: PostgresConfig
) -> AsyncIterator[ChatSocket]:
    credential = local_app.config.credential(ADMIN)
    response = httpx.post(
        f"{local_app.config.base_url}/login",
        data={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    assert response.status_code == HTTPStatus.OK, response.text[:200]
    parts: list[str] = []
    for name, value in response.cookies.items():
        parts.append(f"{name}={value}")

    socket = ChatSocket(local_app, "; ".join(parts), str(uuid4()))
    await socket.open()
    try:
        yield socket
    finally:
        await socket.close()


class SealedQueryTurn:
    """Ход «pg_query с именем соединения» и его шаг в ленте."""

    def __init__(self, tab: ChatSocket) -> None:
        self._tab = tab

    async def run(self) -> dict[str, Any]:
        call = {
            "name": "mcp__boba_general__pg_query",
            "arguments": {
                "connection": CONNECTION,
                "sql": "select 1 as one",
                "offset": 0,
                "limit": 10,
            },
        }
        await self._tab.send(f"{ScenarioName.CALL.value} {json.dumps([call])}")
        await self._tab.wait_task_end()
        assert not self._tab.errors(), self._tab.errors()

        return ToolSteps(self._tab).named("pg_query")[-1]


class TestSignInOfTheAgent:
    """Каким входом чат приходит на сервис за агента, решает конфиг чата."""

    async def test_proxy_signs_in_the_user_of_the_tab(
        self, app: StandProcess, tab: ChatSocket, probe: PostgresConfig
    ) -> None:
        companion = app.companion
        assert companion is not None

        step = await SealedQueryTurn(tab).run()

        assert not step.get("isError"), (step, companion.tail(60))
        assert "one" in str(step.get("output")), step
        log = companion.log_path.read_text(encoding="utf-8", errors="replace")
        assert f"proxy sign-in [user={ADMIN}]" in log, log[-2000:]
        assert f"[user={ServiceStand.AGENT_LOGIN}]" not in log

    async def test_local_signs_in_the_account_of_the_agent(
        self, local_app: StandProcess, local_tab: ChatSocket
    ) -> None:
        companion = local_app.companion
        assert companion is not None

        step = await SealedQueryTurn(local_tab).run()

        assert not step.get("isError"), (step, companion.tail(60))
        assert "one" in str(step.get("output")), step
        log = companion.log_path.read_text(encoding="utf-8", errors="replace")
        assert f"sign-in [user={ServiceStand.AGENT_LOGIN}] [provider=" in log
        assert "proxy sign-in" not in log, log[-2000:]

    async def test_chat_renews_the_expired_token_of_the_service(
        self, local_app: StandProcess, local_tab: ChatSocket
    ) -> None:
        companion = local_app.companion
        assert companion is not None
        turn = SealedQueryTurn(local_tab)
        first = await turn.run()

        await asyncio.sleep(SHORT_TOKEN_SEC + 2)
        second = await turn.run()

        assert not first.get("isError"), (first, companion.tail(60))
        assert not second.get("isError"), (second, companion.tail(60))
        assert "one" in str(second.get("output")), second


class TestRelayAcceptance:
    """Приёмка ретранслятора: настоящий `boba acp` v2 зовёт инструменты стенда
    boba-mcp (вход proxy от имени пользователя вкладки) и обычного сервера
    MCP только через mcp/message чата. Что агент предложил модели, видно по
    запросам фейковой модели."""

    SERVERS: ClassVar[tuple[str, ...]] = ("boba", "standard")

    @pytest.fixture
    async def agents(
        self,
        tmp_path: Path,
        model: FakeModel,
        stand: Stand,
        service_process: ServiceProcess,
        standard: McpServerConfig,
    ) -> AsyncIterator[Agents]:
        servers = dict(service_process.servers().servers)
        servers["standard"] = standard
        made = Agents(
            tmp_path / "agents", model, stand, McpServersConfig(servers=servers)
        )
        try:
            yield made
        finally:
            await made.close_all()

    async def _session(
        self, agents: Agents, roles: frozenset[str], thread: str
    ) -> tuple[AgentProcess, RecordingUpdates, str]:
        """Процесс агента вкладки пользователя с ролями roles и его сессия."""
        tab = TabSession()
        tab.password_sign_in(roles)
        servers = agents.registrations.register(
            tab.registration(uuid4(), "general"), self.SERVERS
        )
        updates = RecordingUpdates()
        process = await agents.start(updates, thread, servers)
        opened = await process.open_session()

        return process, updates, opened.session_id

    @staticmethod
    def _call(name: str, arguments: Mapping[str, Any]) -> str:
        spec = {"name": name, "arguments": dict(arguments)}

        return f"{ScenarioName.CALL.value} {json.dumps(spec)}"

    async def test_tool_of_a_foreign_role_is_not_offered(
        self, agents: Agents, model: FakeModel, auth_token: str
    ) -> None:
        names = McpToolNames()
        answer = f"please {ScenarioName.ANSWER.value}"

        wide, _, wide_session = await self._session(agents, frozenset({"dev"}), "dev")
        await agents.turn(wide, wide_session, answer)
        offered_wide = OfferedTools(model.app.requests[-1], names)

        narrow, _, narrow_session = await self._session(
            agents, frozenset({"weak"}), "weak"
        )
        await agents.turn(narrow, narrow_session, answer)
        offered_narrow = OfferedTools(model.app.requests[-1], names)

        assert offered_wide.named("fake_whoami") == "mcp__boba__fake_whoami"
        assert offered_narrow.named("fake_echo") == "mcp__boba__fake_echo"
        assert offered_narrow.named("fake_whoami") == "fake_whoami"

    async def test_stop_cancels_the_call_on_the_server(
        self, agents: Agents, tmp_path: Path, auth_token: str
    ) -> None:
        process, updates, session_id = await self._session(
            agents, frozenset({"dev"}), "stop"
        )
        marker = tmp_path / "sleep.pid"
        text = self._call(
            "mcp__boba__fake_sleep", {"seconds": 120, "marker": str(marker)}
        )

        ending = agents.questions[process].ending(session_id)
        await process.prompt(session_id, [schema.TextContentBlock(text=text)])
        deadline = time.monotonic() + 60.0
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

        assert marker.exists(), updates.updates
        body = Path("/proc") / marker.read_text(encoding="utf-8")
        await process.cancel(session_id)
        idle = await asyncio.wait_for(ending, 30.0)

        deadline = time.monotonic() + 30.0
        while body.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

        assert idle.stop_reason == "cancelled"
        assert not body.exists(), f"the body {body} of the call still runs"

    async def test_app_only_tool_is_neither_offered_nor_called(
        self, agents: Agents, model: FakeModel, auth_token: str
    ) -> None:
        process, updates, session_id = await self._session(
            agents, frozenset({"dev"}), "app-only"
        )
        text = self._call("mcp__standard__app_only", {})

        _, idle = await agents.turn(process, session_id, text)

        offered = OfferedTools(model.app.requests[-1], McpToolNames())
        calls: list[schema.SessionToolCallUpdate] = []
        for _, update in updates.updates:
            if isinstance(update, schema.SessionToolCallUpdate):
                calls.append(update)

        assert idle.stop_reason == "end_turn"
        assert offered.named("add") == "mcp__standard__add"
        assert offered.named("app_only") == "app_only"
        assert any(call.status == "failed" for call in calls), calls
        assert not any(call.status == "completed" for call in calls), calls
