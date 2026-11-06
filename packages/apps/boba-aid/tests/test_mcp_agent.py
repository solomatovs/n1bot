"""Личный MCP-сервер меняет набор агентов пользователя и историю инструментов."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from uuid import uuid4

import pytest
from acp_stand import AppsHost
from psycopg import sql
from psycopg.types.json import Jsonb
from test_acp_chat import AgentProcesses, ChatRows, ChatSocket
from test_acp_chat import app as app
from test_acp_chat import cookie as cookie
from test_acp_chat import fake_llm as fake_llm
from test_acp_chat import llm_port as llm_port
from test_acp_chat import rows as rows
from test_acp_chat import second as second
from test_acp_chat import tab as tab

from boba.agent.records import DeferredToolsDeltaAttachment
from boba.aid.config.app import AppConfig
from boba.aid.config.models import PostgresAgentHistory, ProcessAgentConfig
from boba.aid.domain.fields import ThreadMetaField
from boba.aid.mcp_servers.store import StoredServer
from boba.config import bind
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.mcp_client.client import HttpEndpoint, McpServerConfig, SseEndpoint
from boba.stand.names import StandNames
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandApp, StandProcess

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Сессия chainlit живёт в настоящем процессе приложения."""


@pytest.fixture(scope="module")
def stand_database() -> str:
    return StandDatabase(
        StandApp.AID, StandNames().tree_database("mcp_own_agent")
    ).prepare()


@pytest.fixture(scope="module")
def live_host() -> Iterator[AppsHost]:
    host = AppsHost([])
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


class ToolDeltas:
    """Читает вложения изменения инструментов из настоящей истории агента."""

    def __init__(self, config: AppConfig) -> None:
        self._pool = AsyncPostgresPool(config.postgres)
        agent = config.agent
        assert isinstance(agent, ProcessAgentConfig)
        history = agent.history
        assert isinstance(history, PostgresAgentHistory)
        self._known_query = sql.SQL("""
            select exists(select 1 from {}.session_records
                          where session_id = %s and position(%s in record::text) > 0)
        """).format(sql.Identifier(history.db_schema))
        self._query = sql.SQL("""
            select record -> 'attachment' from {}.session_records
            where session_id = %s
                and record #>> '{{attachment,type}}' = 'deferred_tools_delta'
            order by seq
        """).format(sql.Identifier(history.db_schema))

    async def read(self, session_id: str) -> Sequence[DeferredToolsDeltaAttachment]:
        await self._pool.open()
        try:
            async with self._pool.connection() as conn:
                cursor = await conn.execute(self._query, (session_id,))
                records = await cursor.fetchall()

            deltas = []
            for row in records:
                deltas.append(DeferredToolsDeltaAttachment.model_validate(row[0]))

            return deltas
        finally:
            await self._pool.close()

    async def knows(self, session_id: str, tool: str) -> bool:
        await self._pool.open()
        try:
            async with self._pool.connection() as conn:
                cursor = await conn.execute(self._known_query, (session_id, tool))
                row = await cursor.fetchone()

            assert row is not None

            return bool(row[0])
        finally:
            await self._pool.close()


class AdminRoleServer:
    """Вставляет сервер и выдачу ролям SQL-запросами без действий приложения."""

    def __init__(self, config: AppConfig) -> None:
        self._pool = AsyncPostgresPool(config.postgres)
        self._cipher = SecretCipher(config.mcp_servers.cipher.get_secret_value())
        schema = sql.Identifier(config.db_schema)
        self._insert = sql.SQL(
            "insert into {}.mcp_servers(name,data) values (%s,%s) returning id"
        ).format(schema)
        self._grant = sql.SQL("""
            insert into {}.grants (src_kind,src_kind_id,tgt_kind,tgt_kind_id)
            select 'mcp_servers',%s,'roles',id from {}.roles
        """).format(schema, schema)

    async def add(self, name: str, server: StoredServer) -> None:
        await self._pool.open()
        try:
            async with self._pool.connection() as conn, conn.transaction():
                cursor = await conn.execute(
                    self._insert, (name, Jsonb(self._cipher.encrypt(server)))
                )
                row = await cursor.fetchone()
                assert row is not None
                await conn.execute(self._grant, (row[0],))
        finally:
            await self._pool.close()


@dataclass(frozen=True)
class AgentPeer:
    tab: ChatSocket
    processes: AgentProcesses


@pytest.fixture
async def peer(
    second: StandProcess, cookie: str, live_host: AppsHost
) -> AsyncIterator[AgentPeer]:
    """Закрывает удалённую сессию до остановки её MCP-сервера."""
    remote = ChatSocket(second, cookie, str(uuid4()))
    await remote.open()
    try:
        yield AgentPeer(remote, AgentProcesses(second))
    finally:
        await remote.close()


async def test_own_server_restarts_all_user_agents_and_records_delta(
    app: StandProcess,
    tab: ChatSocket,
    peer: AgentPeer,
    rows: ChatRows,
    live_host: AppsHost,
) -> None:
    processes = AgentProcesses(app)
    remote_processes = peer.processes
    remote = peer.tab

    for socket in (tab, remote):
        await socket.send(f"before adding own MCP {ScenarioName.ANSWER.value}")
        await socket.wait_task_end()
        assert not socket.errors(), socket.trace()

    first_pid = await processes.await_thread(tab.thread_id)
    second_pid = await remote_processes.await_thread(remote.thread_id)
    endpoint = McpServerConfig.model_validate(live_host.plain()).endpoint.label()
    answer = await asyncio.to_thread(
        tab.action,
        "mcp_add",
        {
            "form": {
                "name": "personal",
                "transport": "streamable-http",
                "url": endpoint,
                "auth": "none",
                "token": "",
                "headers": [],
            }
        },
    )
    assert "error" not in answer, answer
    assert first_pid in processes.of_thread(tab.thread_id)
    await remote.wait_log("MCP server update for user")
    for socket in (tab, remote):
        await socket.send(f"after adding own MCP {ScenarioName.ANSWER.value}")
        await socket.wait_task_end()
        assert not socket.errors(), socket.trace()

    await processes.await_thread(tab.thread_id, other_than=first_pid)
    await remote_processes.await_thread(remote.thread_id, other_than=second_pid)
    config = bind(
        StandApp.AID.config_files().loader(app.config.files.paths(StandApp.AID)).load(),
        "app",
        AppConfig,
    )
    for socket in (tab, remote):
        meta = await rows.thread_meta(socket.thread_id)
        assert meta is not None
        deltas = await ToolDeltas(config).read(str(meta[ThreadMetaField.AGENT_SESSION]))
        added = set()
        for delta in deltas:
            added.update(delta.added_names)
            added.update(delta.surfaced_names)

        assert "mcp__personal__add" in added, deltas

    removed = await asyncio.to_thread(tab.action, "mcp_remove", {"name": "personal"})
    assert "error" not in removed, removed
    await tab.send(f"after removing own MCP {ScenarioName.ANSWER.value}")
    await tab.wait_task_end()
    assert not tab.errors(), tab.trace()
    meta = await rows.thread_meta(tab.thread_id)
    assert meta is not None
    deltas = await ToolDeltas(config).read(str(meta[ThreadMetaField.AGENT_SESSION]))
    gone = set()
    for delta in deltas:
        gone.update(delta.removed_names)

    assert "mcp__personal__add" in gone, deltas


async def test_sql_role_grant_appears_only_in_a_new_chat(
    app: StandProcess,
    tab: ChatSocket,
    cookie: str,
    rows: ChatRows,
    live_host: AppsHost,
) -> None:
    await tab.send(f"before role grant {ScenarioName.ANSWER.value}")
    await tab.wait_task_end()
    config = bind(
        StandApp.AID.config_files().loader(app.config.files.paths(StandApp.AID)).load(),
        "app",
        AppConfig,
    )
    server = McpServerConfig.model_validate(live_host.plain())
    assert isinstance(server.endpoint, (HttpEndpoint, SseEndpoint))
    stored = StoredServer(endpoint=server.endpoint, limits=server.limits)
    await AdminRoleServer(config).add("role_later", stored)
    added = await asyncio.to_thread(
        tab.action,
        "mcp_add",
        {
            "form": {
                "name": "personal_probe",
                "transport": "streamable-http",
                "url": server.endpoint.label(),
                "auth": "none",
                "token": "",
                "headers": [],
            }
        },
    )
    assert "error" not in added, added
    await tab.send(f"after role grant {ScenarioName.ANSWER.value}")
    await tab.wait_task_end()
    assert not tab.errors(), tab.trace()
    before = await rows.thread_meta(tab.thread_id)
    assert before is not None
    assert not await ToolDeltas(config).knows(
        str(before[ThreadMetaField.AGENT_SESSION]), "mcp__role_later__add"
    )
    assert await ToolDeltas(config).knows(
        str(before[ThreadMetaField.AGENT_SESSION]), "mcp__personal_probe__add"
    )
    fresh = ChatSocket(app, cookie, str(uuid4()))
    await fresh.open()
    try:
        await fresh.send(f"new chat after role grant {ScenarioName.ANSWER.value}")
        await fresh.wait_task_end()
        assert not fresh.errors(), fresh.trace()
        after = await rows.thread_meta(fresh.thread_id)
        assert after is not None
        assert await ToolDeltas(config).knows(
            str(after[ThreadMetaField.AGENT_SESSION]), "mcp__role_later__add"
        )
    finally:
        await fresh.close()
        removed = await asyncio.to_thread(
            tab.action, "mcp_remove", {"name": "personal_probe"}
        )
        assert "error" not in removed, removed


async def test_role_connection_rejects_direct_mutation_actions(tab: ChatSocket) -> None:
    before = await asyncio.to_thread(tab.action, "mcp_form", {"name": "aid_all"})
    assert "error" not in before, before
    assert not before["editable"]
    assert before["auth"] == "proxy"
    attempted = await asyncio.to_thread(
        tab.action,
        "mcp_update",
        {
            "current": "aid_all",
            "form": {
                "name": "hijacked",
                "transport": "streamable-http",
                "url": "https://different.invalid/mcp",
                "auth": "none",
                "token": "",
                "headers": [],
            },
        },
    )
    assert "personally" in attempted["error"], attempted
    toggled = await asyncio.to_thread(
        tab.action, "mcp_toggle", {"name": "aid_all", "enabled": False}
    )
    assert "personally" in toggled["error"], toggled
    removed = await asyncio.to_thread(tab.action, "mcp_remove", {"name": "aid_all"})
    assert "personally" in removed["error"], removed
    after = await asyncio.to_thread(tab.action, "mcp_form", {"name": "aid_all"})
    assert after == before
