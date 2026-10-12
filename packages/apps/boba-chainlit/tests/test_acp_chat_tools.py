"""Чат как сервер инструментов своего агента: стенд chainlit с включёнными
`/chat/mcp` и обработчиком хука PreToolUse, сервис boba-mcp стенда с плагином
pg процессами хоста, настоящий `boba acp` с фейковой моделью.

Проверяется ход «connection_list на /chat/mcp → pg_query на boba-mcp с именем
соединения»: обработчик хука подменяет имя запечатанным профилем, сервис
открывает его и исполняет запрос, в ленте остаётся имя; сам сервер `/chat/mcp`
отдаёт инструменты соединений клиенту MCP с ключом процесса агента и отвечает
401 всему остальному, включая JWT входа пользователя; обработчик хука отвечает
`allow` с `updatedInput`, `deny` на неизвестное соединение и 401 без годного
ключа. Ключ живёт с процессом: перезапуск агента и закрытие вкладки его
снимают. Серверы профиля агент получает файлом настроек со способом входа
из конфига чата: proxy — от имени пользователя вкладки, local — учёткой
агента."""

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
import httpx2
import jwt
import pytest
from acp_stand import AgentHistoryStand
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_acp_chat import BOOT_TIMEOUT_SEC, AgentProcesses, ChatSocket

import boba.chainlit.agent
from boba.agent.hooks import (
    HookDecision,
    HookReplyBody,
    McpServerInfo,
    PreToolUseEvent,
)
from boba.agent.records import PermissionMode
from boba.chainlit.rendering.chat_view import StepStatus
from boba.connections.sealed import SealedParamSchema, SealFeature, SealKeys
from boba.db.postgres.connection import PostgresConfig
from boba.identity.token import ClaimKey
from boba.runtime.config import RuntimeConfig
from boba.runtime.http import RequestTokens
from boba.stand.names import StandNames
from boba.stand.site import Stand
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import ScenarioName, serve
from boba.stand.ui.stand import (
    ServiceStand,
    StandAgentAuth,
    StandApp,
    StandConfig,
    StandProcess,
    StandUrl,
    free_port,
)

pytestmark = pytest.mark.anyio

DB_NAME = "boba_acp_tools"
TOKEN_DELAY_SEC = 0.0
CONNECTION = "probe"
SERVICE_PLUGINS = ("pg",)
MCP_SERVER = "boba_general"
ADMIN = "admin"
JSON_RPC = "2.0"
SHORT_TOKEN_SEC = 4
"""Срок токена сервиса на стенде входа local: агент успевает его пережить."""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Стенд живёт отдельным процессом: сессия chainlit в тесте не нужна."""


@pytest.fixture(scope="module")
def stand_database() -> StandDatabase:
    return StandDatabase(StandApp.CHAINLIT, StandNames().of(DB_NAME))


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
    stand: Stand,
) -> Iterator[StandProcess]:
    workdir = tmp_path_factory.mktemp("acp-tools-stand")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=db_name,
        agent_history=AgentHistoryStand(stand).document(),
        agent_chat_mcp=True,
        agent_hook=True,
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


class Addresses:
    """Адреса входов чата для агента на стенде."""

    def __init__(self, app: StandProcess) -> None:
        self._base = app.config.base_url

    def mcp(self) -> str:
        return f"{self._base}/chat/mcp"

    def hook(self) -> str:
        return f"{self._base}/chat/hooks/pre-tool-use"


@pytest.fixture(scope="module")
def addresses(app: StandProcess) -> Addresses:
    return Addresses(app)


class AgentFiles:
    """Файл настроек процесса агента вкладки: то, что чат отдал агенту."""

    def __init__(self, app: StandProcess) -> None:
        self._agents = AgentProcesses(app)

    async def pid(self, thread_id: str, other_than: int = 0) -> int:
        return await self._agents.await_thread(thread_id, other_than)

    def pids(self) -> list[int]:
        return self._agents.pids()

    async def text(self, thread_id: str, other_than: int = 0) -> str:
        pid = await self.pid(thread_id, other_than)
        path = self._agents.of_thread(thread_id)[pid]

        return path.read_text(encoding="utf-8")

    async def document(self, thread_id: str, other_than: int = 0) -> dict[str, Any]:
        return json.loads(await self.text(thread_id, other_than))

    async def key(self, thread_id: str, other_than: int = 0) -> str:
        """Ключ процесса агента треда из записи сервера чата его файла."""
        document = await self.document(thread_id, other_than)
        key = document["mcpServers"]["chat"]["auth"]["token"]
        assert isinstance(key, str)
        assert key

        return key


@pytest.fixture
async def process_key(app: StandProcess, tab: ChatSocket) -> str:
    """Ключ процесса агента открытой вкладки."""
    return await AgentFiles(app).key(tab.thread_id)


class Bearer:
    """Заголовок Authorization с токеном носителя."""

    @staticmethod
    def headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


class ForgedTokens:
    """Токены, которые чат принимать не должен: чужой подписи и просроченный."""

    ALGORITHM: ClassVar[str] = "HS256"

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def foreign(self) -> str:
        return self._encoded("not-the-chat-secret", int(time.time()) + 600)

    def expired(self) -> str:
        return self._encoded(self._secret, int(time.time()) - 60)

    def headers(self, kind: str, sign_in: str) -> dict[str, str]:
        """Заголовки запроса с токеном вида kind, который ключом процесса не
        является: чужой подписи, просроченный, JWT входа пользователя,
        случайная строка; none — без заголовка."""
        tokens = {
            "foreign": self.foreign(),
            "expired": self.expired(),
            "sign_in": sign_in,
            "random": uuid4().hex,
        }
        if kind not in tokens:
            return {}

        return Bearer.headers(tokens[kind])

    def _encoded(self, secret: str, exp: int) -> str:
        claims = {
            ClaimKey.IDENTIFIER.value: ADMIN,
            ClaimKey.METADATA.value: {},
            ClaimKey.EXP.value: exp,
        }

        return jwt.encode(claims, secret, algorithm=self.ALGORITHM)


class McpTab:
    """Клиент MCP теста на `/chat/mcp`: тот же транспорт, что у агента."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token

    async def tool_names(self) -> list[str]:
        async with self._session() as session:
            listed = await session.list_tools()

        return sorted(tool.name for tool in listed.tools)

    async def call_text(self, name: str, arguments: Mapping[str, Any]) -> str:
        async with self._session() as session:
            result = await session.call_tool(name, dict(arguments))

        assert not result.is_error, result
        texts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                texts.append(text)

        return "\n".join(texts)

    def _session(self) -> _OpenedSession:
        return _OpenedSession(self._url, self._token)


class _OpenedSession:
    """Сессия MCP на время блока: http-клиент с Bearer, потоки, initialize."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._http: httpx2.AsyncClient | None = None
        self._streams: Any = None
        self._session: Any = None

    async def __aenter__(self) -> ClientSession:
        self._http = httpx2.AsyncClient(headers=Bearer.headers(self._token))
        self._streams = streamable_http_client(self._url, http_client=self._http)
        read, write = await self._streams.__aenter__()
        self._session = ClientSession(read, write)
        session = await self._session.__aenter__()
        await session.initialize()

        return session

    async def __aexit__(self, *exc: object) -> None:
        await self._session.__aexit__(*exc)
        await self._streams.__aexit__(*exc)
        if self._http is not None:
            await self._http.aclose()


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
    """Агент стенда зовёт connection_list на /chat/mcp и pg_query на boba-mcp."""

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

        app_log = _lines_since(app, since)
        assert any(
            f"hook: connection {CONNECTION} of kind postgres sealed in connection "
            "of mcp__boba_general__pg_query" in line
            for line in app_log
        ), "\n".join(app_log[-40:])

        service_log = _lines_since(companion, service_since)
        assert any(
            "sealed connection in connection (postgres)" in line for line in service_log
        ), "\n".join(service_log[-40:])

        assert not app.complaints(since), app.complaints(since)


def _lines_since(process: StandProcess[Any], since: int) -> list[str]:
    text = process.log_path.read_text(encoding="utf-8", errors="replace")

    return text.splitlines()[since:]


class TestChatMcp:
    """Сервер инструментов чата глазами клиента MCP."""

    async def test_lists_and_calls_connection_tools_with_the_process_key(
        self, addresses: Addresses, process_key: str, probe: PostgresConfig
    ) -> None:
        client = McpTab(addresses.mcp(), process_key)

        names = await client.tool_names()
        assert names == ["connection_list", "connection_search"]

        text = await client.call_text("connection_list", {})
        assert CONNECTION in text
        assert probe.dbname in text

        found = await client.call_text("connection_search", {"kind": "postgres"})
        assert CONNECTION in found

    @pytest.mark.parametrize(
        "token", ["foreign", "expired", "none", "sign_in", "random"]
    )
    async def test_rejects_everything_that_is_not_a_process_key(
        self,
        addresses: Addresses,
        runtime_config: RuntimeConfig,
        session_jwt: str,
        process_key: str,
        token: str,
    ) -> None:
        forged = ForgedTokens(runtime_config.session.auth_secret)
        headers = forged.headers(token, session_jwt)

        response = httpx.post(
            addresses.mcp(),
            json={
                "jsonrpc": JSON_RPC,
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            headers={
                **headers,
                "Accept": "application/json, text/event-stream",
            },
            timeout=30.0,
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text[:300]


class HookEvents:
    """События PreToolUse по форме агента для прямых запросов обработчику."""

    def __init__(self, keys: SealKeys) -> None:
        self._keys = keys
        self._declared = SealedParamSchema()

    def pg_query(self, connection: str, server: McpServerInfo | None) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "connection": {"type": "string", **self._declared.declared("postgres")},
                "sql": {"type": "string"},
            },
            "required": ["connection", "sql"],
        }

        return self._event(
            "mcp__boba_general__pg_query",
            {"connection": connection, "sql": "select 1"},
            schema,
            server,
        )

    def plain(self) -> dict[str, Any]:
        schema = {"type": "object", "properties": {"text": {"type": "string"}}}

        return self._event(
            "mcp__chat__connection_search", {"text": "hi"}, schema, self.server()
        )

    def server(self) -> McpServerInfo:
        return McpServerInfo(
            name=MCP_SERVER,
            features={SealFeature.ID: self._keys.feature().model_dump()},
        )

    @staticmethod
    def featureless() -> McpServerInfo:
        return McpServerInfo(name=MCP_SERVER, features={})

    @staticmethod
    def _event(
        tool: str,
        arguments: Mapping[str, Any],
        schema: Mapping[str, Any],
        server: McpServerInfo | None,
    ) -> dict[str, Any]:
        event = PreToolUseEvent(
            session_id=str(uuid4()),
            cwd="/tmp/agent",  # noqa: S108 — путь события, не файл теста
            permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            tool_name=tool,
            tool_input=dict(arguments),
            tool_use_id="toolu_01",
            tool_schema=dict(schema),
            mcp_server=server,
        )

        return event.model_dump(mode="json")


class TestHookHandler:
    """Обработчик хука PreToolUse по прямому запросу с ключом процесса."""

    async def test_seals_a_granted_connection(
        self, addresses: Addresses, process_key: str, probe: PostgresConfig
    ) -> None:
        keys = SealKeys()
        events = HookEvents(keys)
        response = httpx.post(
            addresses.hook(),
            json=events.pg_query(CONNECTION, events.server()),
            headers=Bearer.headers(process_key),
            timeout=30.0,
        )
        assert response.status_code == HTTPStatus.OK, response.text[:300]

        reply = HookReplyBody.model_validate(response.json())
        output = reply.hook_specific_output
        assert output is not None
        assert output.permission_decision is HookDecision.ALLOW
        assert output.updated_input is not None
        assert output.updated_input["sql"] == "select 1"

        sealed = output.updated_input["connection"]
        assert sealed != CONNECTION
        opened = keys.open(sealed)
        assert opened.name == CONNECTION
        assert opened.profile["dbname"] == probe.dbname

    async def test_denies_an_unknown_connection(
        self, addresses: Addresses, process_key: str, probe: PostgresConfig
    ) -> None:
        events = HookEvents(SealKeys())
        response = httpx.post(
            addresses.hook(),
            json=events.pg_query("nobody-granted-this", events.server()),
            headers=Bearer.headers(process_key),
            timeout=30.0,
        )
        assert response.status_code == HTTPStatus.OK, response.text[:300]

        output = HookReplyBody.model_validate(response.json()).hook_specific_output
        assert output is not None
        assert output.permission_decision is HookDecision.DENY
        assert "pg_query" in output.permission_decision_reason
        assert CONNECTION in output.permission_decision_reason
        assert output.updated_input is None

    async def test_denies_when_the_server_declares_no_seal_key(
        self, addresses: Addresses, process_key: str, probe: PostgresConfig
    ) -> None:
        events = HookEvents(SealKeys())
        response = httpx.post(
            addresses.hook(),
            json=events.pg_query(CONNECTION, events.featureless()),
            headers=Bearer.headers(process_key),
            timeout=30.0,
        )
        assert response.status_code == HTTPStatus.OK, response.text[:300]

        output = HookReplyBody.model_validate(response.json()).hook_specific_output
        assert output is not None
        assert output.permission_decision is HookDecision.DENY
        assert SealFeature.ID in output.permission_decision_reason

    async def test_allows_tools_without_connections_unchanged(
        self, addresses: Addresses, process_key: str
    ) -> None:
        events = HookEvents(SealKeys())
        response = httpx.post(
            addresses.hook(),
            json=events.plain(),
            headers=Bearer.headers(process_key),
            timeout=30.0,
        )
        assert response.status_code == HTTPStatus.OK, response.text[:300]

        body = response.json()
        output = HookReplyBody.model_validate(body).hook_specific_output
        assert output is not None
        assert output.permission_decision is HookDecision.ALLOW
        assert "updatedInput" not in body["hookSpecificOutput"]

    @pytest.mark.parametrize(
        "token", ["foreign", "expired", "none", "sign_in", "random"]
    )
    async def test_rejects_everything_that_is_not_a_process_key(
        self,
        addresses: Addresses,
        runtime_config: RuntimeConfig,
        session_jwt: str,
        process_key: str,
        token: str,
    ) -> None:
        forged = ForgedTokens(runtime_config.session.auth_secret)
        headers = forged.headers(token, session_jwt)

        events = HookEvents(SealKeys())
        response = httpx.post(
            addresses.hook(),
            json=events.pg_query(CONNECTION, events.server()),
            headers=headers,
            timeout=30.0,
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text[:300]
        assert response.headers.get("WWW-Authenticate") == "Bearer"


class ProcessKeyProbe:
    """Запросы к входам чата для агента с названным ключом: по статусу видно,
    принят ли ключ."""

    def __init__(self, addresses: Addresses) -> None:
        self._addresses = addresses
        self._events = HookEvents(SealKeys())

    def hook(self, key: str) -> int:
        response = httpx.post(
            self._addresses.hook(),
            json=self._events.plain(),
            headers=Bearer.headers(key),
            timeout=30.0,
        )

        return response.status_code

    def mcp(self, key: str) -> int:
        response = httpx.post(
            self._addresses.mcp(),
            json={"jsonrpc": JSON_RPC, "id": 1, "method": "tools/list", "params": {}},
            headers={
                **Bearer.headers(key),
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        return response.status_code


class TestProcessKey:
    """Ключ процесса агента: выдан запуском, живёт с процессом."""

    FORM: ClassVar[dict[str, Any]] = {
        "agent_base_url": "",
        "agent_api_key": "",
        "agent_model": "fake-model-rekeyed",
        "session_mode": "bypassPermissions",
        "session_model": "general",
    }
    """Форма панели настроек: её правка перезапускает агента вкладки."""

    async def test_settings_file_carries_the_key_and_no_sign_in_token(
        self, app: StandProcess, tab: ChatSocket, session_jwt: str, process_key: str
    ) -> None:
        files = AgentFiles(app)

        text = await files.text(tab.thread_id)
        document = json.loads(text)

        assert session_jwt not in text
        hook = document["hooks"]["PreToolUse"][0]["hooks"][0]
        assert hook["headers"] == Bearer.headers(process_key)
        assert document["mcpServers"]["chat"]["auth"] == {
            "auth": "bearer",
            "token": process_key,
        }

    async def test_proxy_server_of_the_file_names_the_user_of_the_tab(
        self, app: StandProcess, tab: ChatSocket
    ) -> None:
        document = await AgentFiles(app).document(tab.thread_id)

        auth = document["mcpServers"][MCP_SERVER]["auth"]
        assert auth["auth"] == "proxy"
        assert auth["login"] == ADMIN
        assert auth["roles"] == ["ADM"]
        assert auth["clientId"] == ServiceStand.CLIENT_ID
        assert "headers" not in document["mcpServers"][MCP_SERVER]

    async def test_each_tab_gets_its_own_key(
        self, app: StandProcess, cookie: str, addresses: Addresses, tab: ChatSocket
    ) -> None:
        files = AgentFiles(app)
        other = ChatSocket(app, cookie, str(uuid4()))
        await other.open()
        try:
            first = await files.key(tab.thread_id)
            second = await files.key(other.thread_id)
        finally:
            await other.end_session()

        probe = ProcessKeyProbe(addresses)
        assert first != second
        assert probe.hook(first) == HTTPStatus.OK

    async def test_key_of_a_restarted_agent_is_refused(
        self, app: StandProcess, tab: ChatSocket, addresses: Addresses
    ) -> None:
        files = AgentFiles(app)
        probe = ProcessKeyProbe(addresses)
        old_pid = await files.pid(tab.thread_id)
        old_key = await files.key(tab.thread_id)
        assert probe.hook(old_key) == HTTPStatus.OK
        assert probe.mcp(old_key) == HTTPStatus.OK

        await tab.settings(dict(self.FORM))
        new_key = await files.key(tab.thread_id, other_than=old_pid)

        assert new_key != old_key
        assert probe.hook(old_key) == HTTPStatus.UNAUTHORIZED
        assert probe.mcp(old_key) == HTTPStatus.UNAUTHORIZED
        assert probe.hook(new_key) == HTTPStatus.OK
        assert probe.mcp(new_key) == HTTPStatus.OK

    async def test_key_of_a_closed_tab_is_refused(
        self,
        app: StandProcess,
        cookie: str,
        addresses: Addresses,
        probe: PostgresConfig,
    ) -> None:
        files = AgentFiles(app)
        keys = ProcessKeyProbe(addresses)
        closing = ChatSocket(app, cookie, str(uuid4()))
        await closing.open()
        pid = await files.pid(closing.thread_id)
        key = await files.key(closing.thread_id)
        assert keys.hook(key) == HTTPStatus.OK

        await closing.end_session()
        deadline = time.monotonic() + 60.0
        while pid in files.pids() and time.monotonic() < deadline:
            time.sleep(0.3)

        assert pid not in files.pids(), app.tail(40)
        assert keys.hook(key) == HTTPStatus.UNAUTHORIZED
        assert keys.mcp(key) == HTTPStatus.UNAUTHORIZED


class TestNoTokenExchangeInTheChat:
    """Чат формирует файл и не получает токенов сервиса для агента."""

    async def test_agent_package_has_no_token_exchange(self) -> None:
        root = Path(boba.chainlit.agent.__path__[0])
        modules: list[str] = []
        sources: list[str] = []
        for path in sorted(root.glob("*.py")):
            modules.append(path.name)
            sources.append(path.read_text(encoding="utf-8"))

        assert "tokens.py" not in modules, modules
        for gone in ("McpAccessToken", "McpTokenError", "jwt-bearer", "token_endpoint"):
            assert gone not in "\n".join(sources), gone

    async def test_sessions_are_opened_without_servers_and_tokens(
        self, app: StandProcess, tab: ChatSocket, probe: PostgresConfig
    ) -> None:
        since = app.log_lines()
        await tab.send(f"answer-{uuid4().hex} {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))

        log = "\n".join(_lines_since(app, since))
        assert "mcp token issued" not in log, log[-2000:]


@pytest.fixture(scope="module")
def local_app(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    db_name: str,
    stand: Stand,
) -> Iterator[StandProcess]:
    """Второй стенд чата над той же базой: на сервис он и его агент входят
    учёткой агента (local), а токен сервиса живёт несколько секунд."""
    workdir = tmp_path_factory.mktemp("acp-tools-local")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=db_name,
        agent_history=AgentHistoryStand(stand).document(),
        agent_chat_mcp=True,
        agent_hook=True,
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
    """Каким входом агент пришёл на сервис, решает конфиг чата."""

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
        document = await AgentFiles(local_app).document(local_tab.thread_id)

        step = await SealedQueryTurn(local_tab).run()

        assert document["mcpServers"][MCP_SERVER]["auth"] == {
            "auth": "local",
            "login": ServiceStand.AGENT_LOGIN,
            "password": ServiceStand.AGENT_PASSWORD,
        }
        assert not step.get("isError"), (step, companion.tail(60))
        assert "one" in str(step.get("output")), step
        log = companion.log_path.read_text(encoding="utf-8", errors="replace")
        assert f"sign-in [user={ServiceStand.AGENT_LOGIN}] [provider=" in log
        assert "proxy sign-in" not in log, log[-2000:]

    async def test_agent_renews_the_expired_token_of_the_service_itself(
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
