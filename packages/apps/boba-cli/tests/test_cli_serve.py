"""Служба агента `boba serve` целиком на стенде: вход proxy → токен ресурса
ACP → initialize → session/new с выбором профиля агента настройкой `model` →
ход на фейковой модели → ответ, по WebSocket и по streamable HTTP. Сессии —
потоки процесса службы, история — в Postgres стенда своим подключением
(вход билетом Kerberos из кэша стенда). Проверяется: способ входа службы в
initialize и auth/login; инструмент клиента по MCP-over-ACP (mcp/message
туда и обратно), в том числе у двух вкладок одного пользователя и разных —
каждый запрос в своё подключение; профили только по ролям; чужая сессия не
видна и не открывается; session/close и закрытие подключения закрывают
сессии; предел сессий; подключение HTTP, брошенное клиентом без DELETE,
закрывается по простою, а живое с открытым потоком — нет; session/cancel
доходит до сессии посреди хода; без токена — 401 и отказ рукопожатия, токен
без профилей — 403."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

import httpx2
import mcp_types as mt
import pytest
from acp.exceptions import RequestError
from acp.experimental.v2 import Client, schema
from acp.http import create_http_stream
from acp.ws import create_websocket_stream
from mcp_types.jsonrpc import ErrorData
from mcp_types.methods import parse_client_request
from omegaconf import DictConfig
from psycopg import sql
from pydantic import AnyUrl
from websockets.exceptions import InvalidStatus

from boba.cli.serve import ServeHistory, ServeMark
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.config import ProcessConfig
from boba.stand.agent import FakeServer
from boba.stand.agent_service import AgentServiceStand, ServiceProfiles
from boba.stand.database import TestDatabase
from boba.stand.ui.fake_llm import FailureName, ScenarioName
from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpVersion,
    ClientConnection,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
    StopReason,
)

pytestmark = pytest.mark.anyio

WAIT_SEC = 60.0


class Profile:
    """Профили агента стенда: MAIN — ролям dev и analyst, SPARE — только dev; модели
    у них разные, по ним видно, какой профиль отвечал."""

    MAIN: ClassVar[str] = "main"
    SPARE: ClassVar[str] = "spare"
    MAIN_MODEL: ClassVar[str] = "stand-main-model"
    SPARE_MODEL: ClassVar[str] = "stand-spare-model"


class StandTools:
    """Сервер инструментов клиента по MCP-over-ACP: один инструмент add,
    отвечает на mcp/message сам моделями mcp_types."""

    SERVER: ClassVar[schema.AcpMcpServer] = schema.AcpMcpServer(
        name="stand", server_id="stand:agent-serve"
    )
    WIRE: ClassVar[Mapping[str, Any]] = {
        "mode": "json",
        "by_alias": True,
        "exclude_none": True,
    }
    ADD: ClassVar[mt.Tool] = mt.Tool(
        name="add",
        description="Adds two numbers.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        },
        annotations=mt.ToolAnnotations(read_only_hint=True),
    )

    def __init__(self) -> None:
        self.calls = 0

    async def answer(self, message: McpMessage) -> McpOutcome:
        request = parse_client_request(
            message.method, McpVersion.V2026_07_28, message.params
        )
        if isinstance(request, mt.DiscoverRequest):
            discovered = mt.DiscoverResult(
                supported_versions=[McpVersion.V2026_07_28.value],
                capabilities=mt.ServerCapabilities(tools=mt.ToolsCapability()),
                ttl_ms=0,
            )
            return McpResultOutcome(result=discovered.model_dump(**self.WIRE))

        if isinstance(request, mt.ListToolsRequest):
            listed = mt.ListToolsResult(tools=[self.ADD], ttl_ms=0)
            return McpResultOutcome(result=listed.model_dump(**self.WIRE))

        if not isinstance(request, mt.CallToolRequest):
            missing = f"Method not found: {request.method}"
            return McpErrorOutcome(error=ErrorData(code=-32601, message=missing))

        self.calls += 1
        arguments: Mapping[str, Any] = {}
        if request.params.arguments is not None:
            arguments = request.params.arguments

        total = int(arguments["a"]) + int(arguments["b"])
        reply = mt.CallToolResult(content=[mt.TextContent(text=str(total))])

        return McpResultOutcome(result=reply.model_dump(**self.WIRE))


class StandClient(Client):
    """Клиент ACP v2 стенда: копит уведомления, ждёт idle сессии, на запрос
    разрешения и вопрос формы отвечает отменой (стенд работает в режиме
    bypassPermissions); MCP-over-ACP со стороны клиента (mcp/connect) не
    объявляет — его серверы отвечают через ClientConnection."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self._changed = asyncio.Event()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))
        self._changed.set()

    async def request_permission(
        self,
        session_id: str,
        title: str,
        options: list[schema.PermissionOption],
        description: str | None = None,
        subject: Any = None,
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        return schema.RequestPermissionResponse(
            outcome=schema.CancelledPermissionOutcome()
        )

    async def create_elicitation(  # noqa: PLR0913 — сигнатура протокола SDK
        self,
        message: str,
        mode: str,
        *,
        session_id: str | None = None,
        request_id: int | str | None = None,
        tool_call_id: str | None = None,
        requested_schema: schema.ElicitationSchema | None = None,
        elicitation_id: str | None = None,
        url: str | AnyUrl | None = None,
        **kwargs: Any,
    ) -> schema.CancelElicitationResponse:
        return schema.CancelElicitationResponse()

    # SDK считает метод протокола, оставленный как есть, неподдержанным
    connect_mcp = Client.connect_mcp
    mcp_message = Client.mcp_message
    notify_mcp = Client.notify_mcp
    disconnect_mcp = Client.disconnect_mcp
    complete_elicitation = Client.complete_elicitation

    def texts(self, session_id: str) -> str:
        found: list[str] = []
        for owner, update in self.updates:
            if owner != session_id:
                continue

            if not isinstance(update, schema.AgentMessageChunk):
                continue

            content = update.content
            if isinstance(content, schema.TextContentBlock):
                found.append(content.text)

        return "".join(found)

    async def idle(self, session_id: str, seen: int) -> schema.IdleSessionStateUpdate:
        """idle сессии среди уведомлений после первых seen."""
        deadline = time.monotonic() + WAIT_SEC
        while True:
            for owner, update in self.updates[seen:]:
                if owner != session_id:
                    continue

                if isinstance(update, schema.IdleSessionStateUpdate):
                    return update

            left = deadline - time.monotonic()
            assert left > 0, f"session {session_id} did not become idle"
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), left)
            except TimeoutError:
                continue


class HistoryStand:
    """История агента в Postgres стенда: своя схема на тест и подключение
    стенда (вход билетом по keytab) с пулом службы."""

    def __init__(self, postgres: PostgresConfig, schema_name: str) -> None:
        pool = postgres.pool.model_copy(update={"min_size": 1, "max_size": 4})
        self.postgres = postgres.model_copy(update={"pool": pool})
        self.schema = schema_name

    @staticmethod
    def layer(max_sessions: int) -> dict[str, Any]:
        """Слой настроек агента службы: права, предел сессий и пределы хода."""
        return {
            "permissions": {"defaultMode": "bypassPermissions", "allowBypass": True},
            "sessions": {"maxOpen": max_sessions},
            "limits": {"toolResultWindowShare": 0.25},
        }


class Connected:
    """Подключение клиента к службе: транспорт SDK, соединение ACP v2,
    клиент и сервер инструментов."""

    def __init__(self, transport: Any, tools: StandTools) -> None:
        self.client = StandClient()
        self.tools = tools
        self.conn = ClientConnection(self.client, tools.answer, transport, None)

    async def initialize(self) -> schema.InitializeResponse:
        return await self.conn.initialize(
            protocol_version=AcpVersion.V2,
            info=schema.Implementation(name="agent-service-tests", version="0"),
            capabilities=schema.ClientCapabilities(),
        )

    async def new_session(self, cwd: Path) -> schema.NewSessionResponse:
        return await self.conn.new_session(
            cwd=str(cwd), mcp_servers=[StandTools.SERVER]
        )

    async def turn(self, session_id: str, text: str) -> schema.IdleSessionStateUpdate:
        seen = len(self.client.updates)
        await self.conn.prompt(
            session_id=session_id, prompt=[schema.TextContentBlock(text=text)]
        )

        return await self.client.idle(session_id, seen)

    async def close(self) -> None:
        await self.conn.close()


@pytest.fixture
async def model() -> AsyncIterator[FakeServer]:
    fake = FakeServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def history(raw_config: DictConfig) -> AsyncIterator[HistoryStand]:
    cfg = bind(raw_config, path="postgres", model=PostgresConfig)
    postgres = TestDatabase.config_of(cfg, await TestDatabase.ensure(cfg))
    name = f"agent_svc_{uuid.uuid4().hex[:12]}"
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        yield HistoryStand(postgres, name)
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
            )
    finally:
        await pool.close()


class Stands:
    """Фабрика служб теста: каждая гасится после теста."""

    def __init__(
        self,
        process: ProcessConfig,
        history: HistoryStand,
        model: FakeServer,
    ) -> None:
        self._process = process
        self._history = history
        self._profiles = (
            ServiceProfiles(f"http://127.0.0.1:{model.port}/v1")
            .add(Profile.MAIN, ["dev", "analyst"], Profile.MAIN_MODEL)
            .add(Profile.SPARE, ["dev"], Profile.SPARE_MODEL)
        )
        self.started: list[AgentServiceStand] = []

    async def start(
        self, max_sessions: int = 8, idle_sec: float = 600.0
    ) -> AgentServiceStand:
        stand = AgentServiceStand(
            self._process.krb,
            ServeHistory(schema=self._history.schema, postgres=self._history.postgres),
            self._history.layer(max_sessions),
            self._profiles,
            idle_sec,
        )
        await stand.start()
        self.started.append(stand)

        return stand


@pytest.fixture
async def stands(
    process_config: ProcessConfig,
    history: HistoryStand,
    model: FakeServer,
    tmp_path: Path,
) -> AsyncIterator[Stands]:
    made = Stands(process_config, history, model)
    try:
        yield made
    finally:
        for stand in made.started:
            await stand.stop()


async def websocket(stand: AgentServiceStand, token: str) -> Connected:
    transport = await create_websocket_stream(
        stand.ws_url(), headers={"Authorization": f"Bearer {token}"}
    )

    return Connected(transport, StandTools())


def http(stand: AgentServiceStand, token: str) -> Connected:
    client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(None)
    )

    return Connected(create_http_stream(stand.url(), client=client), StandTools())


def call(name: str, arguments: Mapping[str, object]) -> str:
    """Вопрос, на который фейковая модель зовёт инструмент."""
    spec = json.dumps({"name": name, "arguments": dict(arguments)})

    return f"{ScenarioName.CALL.value} {spec}"


def model_choice(response: schema.NewSessionResponse) -> tuple[str, list[str]]:
    """Текущий профиль и выбор настройки `model` в ответе session/new."""
    assert response.config_options is not None
    for option in response.config_options:
        if option.config_id != "model":
            continue

        assert isinstance(option, schema.SelectSessionConfigOption)
        values: list[str] = []
        for item in option.options:
            assert isinstance(item, schema.SessionConfigSelectOption)
            values.append(item.value)

        return option.current_value, values

    pytest.fail(f"session/new has no model option: {response.config_options}")


class TestTurns:
    async def test_websocket_turn_with_profile_and_tool(
        self, stands: Stands, model: FakeServer, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        connected = await websocket(stand, await stand.token("alice", "dev"))
        try:
            initialized = await connected.initialize()
            assert initialized.auth_methods is not None
            method = initialized.auth_methods[0]
            assert isinstance(method, schema.AgentAuthMethod)
            assert method.method_id == ServeMark.AUTH_METHOD
            await connected.conn.login(method_id=ServeMark.AUTH_METHOD)

            opened = await connected.new_session(tmp_path)
            current, offered = model_choice(opened)
            assert current == Profile.MAIN
            assert sorted(offered) == [Profile.MAIN, Profile.SPARE]
            await connected.conn.set_config_option(
                config_id="model", session_id=opened.session_id, value=Profile.SPARE
            )

            idle = await connected.turn(opened.session_id, ScenarioName.ANSWER.value)
            assert idle.stop_reason == StopReason.END_TURN
            assert connected.client.texts(opened.session_id)
            assert model.requests()[-1]["model"] == Profile.SPARE_MODEL

            asked = call("mcp__stand__add", {"a": 2, "b": 3})
            idle = await connected.turn(opened.session_id, asked)
            assert idle.stop_reason == StopReason.END_TURN
            assert connected.tools.calls == 1
        finally:
            await connected.close()

        await stand.sessions_drop_to(0)

    async def test_streamable_http_turn(
        self, stands: Stands, model: FakeServer, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        connected = http(stand, await stand.token("carol", "analyst"))
        try:
            await connected.initialize()
            opened = await connected.new_session(tmp_path)
            current, offered = model_choice(opened)
            assert current == Profile.MAIN
            assert offered == [Profile.MAIN]

            asked = call("mcp__stand__add", {"a": 1, "b": 1})
            idle = await connected.turn(opened.session_id, asked)
            assert idle.stop_reason == StopReason.END_TURN
            assert connected.tools.calls == 1
            assert model.requests()[-1]["model"] == Profile.MAIN_MODEL

            await connected.conn.close_session(session_id=opened.session_id)
            await stand.sessions_drop_to(0)
        finally:
            await connected.close()

    async def test_cancel_reaches_the_session(
        self, stands: Stands, model: FakeServer, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        connected = await websocket(stand, await stand.token("alice", "dev"))
        try:
            await connected.initialize()
            opened = await connected.new_session(tmp_path)
            seen = len(connected.client.updates)
            hanging = f"{ScenarioName.ANSWER.value} {FailureName.HANG.value}"
            await connected.conn.prompt(
                session_id=opened.session_id,
                prompt=[schema.TextContentBlock(text=hanging)],
            )
            started = time.monotonic()
            await connected.conn.cancel_session(session_id=opened.session_id)

            idle = await connected.client.idle(opened.session_id, seen)
            assert idle.stop_reason == StopReason.CANCELLED
            assert time.monotonic() - started < model.app.hang_sec
        finally:
            await connected.close()


class TestOwners:
    async def test_session_of_alice_is_invisible_to_bob(
        self, stands: Stands, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        alice = await websocket(stand, await stand.token("alice", "dev"))
        try:
            await alice.initialize()
            opened = await alice.new_session(tmp_path)
            await alice.turn(opened.session_id, ScenarioName.ANSWER.value)
            listed = await alice.conn.list_sessions()
            assert [s.session_id for s in listed.sessions] == [opened.session_id]
        finally:
            await alice.close()

        bob = await websocket(stand, await stand.token("bob", "dev"))
        try:
            await bob.initialize()
            assert (await bob.conn.list_sessions()).sessions == []
            with pytest.raises(RequestError) as resumed:
                await bob.conn.resume_session(
                    session_id=opened.session_id,
                    cwd=str(tmp_path),
                    mcp_servers=[StandTools.SERVER],
                )

            assert resumed.value.code == AcpErrorCode.RESOURCE_NOT_FOUND
        finally:
            await bob.close()

        again = await websocket(stand, await stand.token("alice", "dev"))
        try:
            await again.initialize()
            await again.conn.resume_session(
                session_id=opened.session_id,
                cwd=str(tmp_path),
                mcp_servers=[StandTools.SERVER],
            )
            idle = await again.turn(opened.session_id, ScenarioName.ANSWER.value)
            assert idle.stop_reason == StopReason.END_TURN
        finally:
            await again.close()


class TestSessions:
    async def test_closing_the_connection_closes_its_sessions(
        self, stands: Stands, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        connected = await websocket(stand, await stand.token("alice", "dev"))
        await connected.initialize()
        first = await connected.new_session(tmp_path)
        second = await connected.new_session(tmp_path)
        assert first.session_id != second.session_id
        assert stand.host.sessions() == 2

        await connected.close()

        await stand.sessions_drop_to(0)

    async def test_abandoned_http_connection_is_closed(
        self, stands: Stands, tmp_path: Path
    ) -> None:
        stand = await stands.start(idle_sec=1.0)
        client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {await stand.token('alice', 'dev')}"},
            timeout=httpx2.Timeout(None),
        )
        connected = Connected(
            create_http_stream(stand.url(), client=client), StandTools()
        )
        try:
            await connected.initialize()
            opened = await connected.new_session(tmp_path)
            await asyncio.sleep(3.0)
            assert stand.host.sessions() == 1

            idle = await connected.turn(opened.session_id, ScenarioName.ANSWER.value)
            assert idle.stop_reason == StopReason.END_TURN

            await client.aclose()
            await stand.sessions_drop_to(0)
        finally:
            await connected.close()

    async def test_limit_of_sessions_refuses_a_session(
        self, stands: Stands, tmp_path: Path
    ) -> None:
        stand = await stands.start(max_sessions=1)
        connected = await websocket(stand, await stand.token("alice", "dev"))
        try:
            await connected.initialize()
            await connected.new_session(tmp_path)
            with pytest.raises(RequestError) as refused:
                await connected.new_session(tmp_path)

            assert "its limit is 1" in str(refused.value.data)
        finally:
            await connected.close()

    async def test_sessions_of_two_users_run_side_by_side(
        self, stands: Stands, model: FakeServer, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        alice = await websocket(stand, await stand.token("alice", "dev"))
        carol = http(stand, await stand.token("carol", "analyst"))
        try:
            await alice.initialize()
            await carol.initialize()
            first = await alice.new_session(tmp_path)
            second = await carol.new_session(tmp_path)
            turns = await asyncio.gather(
                alice.turn(first.session_id, ScenarioName.ANSWER.value),
                carol.turn(second.session_id, ScenarioName.ANSWER.value),
            )
            for idle in turns:
                assert idle.stop_reason == StopReason.END_TURN

            assert stand.host.sessions() == 2
        finally:
            await alice.close()
            await carol.close()

        await stand.sessions_drop_to(0)


class TestTabs:
    """Две вкладки — два подключения к службе: запрос mcp/message сессии
    уходит в подключение, которое её открыло, а не в последнее открытое,
    будь вкладки одного пользователя или разных."""

    async def tool_turn(self, tab: Connected, session_id: str, calls: int) -> None:
        asked = call("mcp__stand__add", {"a": 2, "b": 2})
        idle = await asyncio.wait_for(tab.turn(session_id, asked), WAIT_SEC)
        assert idle.stop_reason == StopReason.END_TURN
        assert tab.tools.calls == calls

    async def resumed(self, tab: Connected, session_id: str, cwd: Path) -> None:
        await tab.conn.resume_session(
            session_id=session_id, cwd=str(cwd), mcp_servers=[StandTools.SERVER]
        )

    async def test_two_tabs_of_one_user_answer_their_own_tools(
        self, stands: Stands, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        token = await stand.token("alice", "dev")
        first = http(stand, token)
        second = http(stand, token)
        try:
            await first.initialize()
            opened = await first.new_session(tmp_path)
            await second.initialize()
            other = await second.new_session(tmp_path)

            await self.tool_turn(first, opened.session_id, 1)
            await self.tool_turn(second, other.session_id, 1)
            await asyncio.gather(
                self.tool_turn(first, opened.session_id, 2),
                self.tool_turn(second, other.session_id, 2),
            )

            # сессия открыта на ход: вкладки поочерёдно берут одну и ту же
            await first.conn.close_session(session_id=opened.session_id)
            await self.resumed(second, opened.session_id, tmp_path)
            await self.tool_turn(second, opened.session_id, 3)
            await second.conn.close_session(session_id=opened.session_id)
            await self.resumed(first, opened.session_id, tmp_path)
            await self.tool_turn(first, opened.session_id, 3)
        finally:
            await first.close()
            await second.close()

        await stand.sessions_drop_to(0)

    async def test_tabs_of_two_users_answer_their_own_tools(
        self, stands: Stands, tmp_path: Path
    ) -> None:
        stand = await stands.start()
        alice = http(stand, await stand.token("alice", "dev"))
        carol = http(stand, await stand.token("carol", "analyst"))
        try:
            await alice.initialize()
            first = await alice.new_session(tmp_path)
            await carol.initialize()
            second = await carol.new_session(tmp_path)

            await asyncio.gather(
                self.tool_turn(alice, first.session_id, 1),
                self.tool_turn(carol, second.session_id, 1),
            )
            await self.tool_turn(alice, first.session_id, 2)
        finally:
            await alice.close()
            await carol.close()

        await stand.sessions_drop_to(0)


class TestRefusals:
    async def test_no_token_is_refused_on_both_transports(self, stands: Stands) -> None:
        stand = await stands.start()
        async with httpx2.AsyncClient() as client:
            reply = await client.post(
                stand.url(),
                json={"jsonrpc": "2.0", "id": 0, "method": "initialize"},
            )

        assert reply.status_code == httpx2.codes.UNAUTHORIZED
        challenge = reply.headers["www-authenticate"]
        assert "oauth-protected-resource/acp" in challenge

        with pytest.raises(InvalidStatus) as refused:
            await create_websocket_stream(stand.ws_url())

        assert refused.value.response.status_code == httpx2.codes.FORBIDDEN

    async def test_token_without_profiles_is_forbidden(self, stands: Stands) -> None:
        stand = await stands.start(max_sessions=1)
        token = await stand.token("eve", "nobody")
        async with httpx2.AsyncClient() as client:
            reply = await client.post(
                stand.url(),
                json={"jsonrpc": "2.0", "id": 0, "method": "initialize"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert reply.status_code == httpx2.codes.FORBIDDEN
        assert "no agent profile" in reply.text
        assert stand.host.sessions() == 0
