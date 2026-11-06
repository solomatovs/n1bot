"""SIGHUP/TERM/INT через ОС: два TCP-listener'а, PostgreSQL, OAuth и MCP-вызовы.

Каждый тест запускает отдельный сервер и создаёт уникальную схему в тестовой
базе. Production compose не затрагивается. Инструменты — subprocess-стенд.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString
from uuid import uuid4

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import CallToolResult, TextContent
from psycopg import sql
from psycopg.types.json import Jsonb
from service_stand import CLIENT_ID, CLIENT_SECRET, PROXY_SECRET

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.oauth.assertions import ProxyAssertions
from boba.stand.migrate import StandMigrations

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@dataclass(frozen=True)
class InvalidEndpoint:
    """Запись, допустимая для SQL-таблицы, но запрещённая контрактом приложения."""

    path: str
    tools: tuple[str | int, ...]
    reason: str


class CatalogDatabase:
    """Меняет только уникальную схему этого теста; SQL читает настоящий репозиторий."""

    def __init__(self, pool: AsyncPostgresPool, schema: str) -> None:
        self.pool = pool
        self.schema = schema
        self._name = sql.Identifier(schema)

    async def execute(
        self, statement: LiteralString, params: Sequence[object] = ()
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql.SQL(statement).format(self._name), params)

    async def seed(self) -> None:
        await self.execute("insert into {}.roles (role) values ('dev'), ('weak')")
        await self.endpoint("/service", ("dev", "weak"))
        await self.endpoint("/narrow", ("dev",))

    async def endpoint(self, path: str, roles: Sequence[str]) -> None:
        tools = Jsonb(["fake_whoami", "fake_sleep", "fake_gate", "fake_echo"])
        await self.execute(
            "insert into {}.mcp_endpoints (prefix_url, title, tools) values (%s,%s,%s)",
            (path, path, tools),
        )
        for role in roles:
            await self.execute(
                "insert into {0}.grants (src_kind, src_kind_id, tgt_kind, tgt_kind_id) "
                "select 'mcp_endpoints', e.id, 'roles', r.id "
                "from {0}.mcp_endpoints e cross join {0}.roles r "
                "where e.prefix_url=%s and r.role=%s",
                (path, role),
            )

    async def invalidate(self, endpoint: InvalidEndpoint) -> None:
        await self.execute(
            "insert into {}.mcp_endpoints (prefix_url, title, tools) values (%s,%s,%s)",
            (endpoint.path, "invalid endpoint", Jsonb(endpoint.tools)),
        )

    async def snapshot(self) -> str:
        """Снимок строк и grants для проверки отсутствия записи со стороны reload."""
        statement = sql.SQL(
            "select jsonb_build_object("
            "'endpoints', (select jsonb_agg(to_jsonb(e) order by e.id) "
            "from {0}.mcp_endpoints e), "
            "'grants', (select jsonb_agg(to_jsonb(g) order by g.id) "
            "from {0}.grants g))::text"
        ).format(self._name)
        async with self.pool.connection() as conn:
            cursor = await conn.execute(statement)
            row = await cursor.fetchone()

        assert row is not None
        assert isinstance(row[0], str)
        return row[0]


class NetworkServer:
    """Владеет subprocess и двумя сокетами; даже упавший тест освобождает процесс."""

    def __init__(self, workdir: Path, dbname: str, schema: str) -> None:
        self.workdir = workdir
        self._log_path = workdir / "reload.log"
        self._log = self._log_path.open("wb")
        self._sockets = [socket.socket(), socket.socket()]
        for listener in self._sockets:
            listener.bind(("127.0.0.1", 0))

        self.urls = [f"http://127.0.0.1:{s.getsockname()[1]}" for s in self._sockets]
        fds = tuple(s.fileno() for s in self._sockets)
        arguments = [sys.executable, str(Path(__file__).with_name("reload_process.py"))]
        arguments.extend([str(workdir), dbname, schema])
        arguments.extend(map(str, fds))
        self.process = subprocess.Popen(
            arguments,
            pass_fds=fds,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for listener in self._sockets:
            listener.close()

        self.http = httpx.AsyncClient(timeout=5)
        self._assertions = ProxyAssertions(PROXY_SECRET, 0)

    def log(self) -> str:
        return self._log_path.read_text(errors="replace")

    async def ready(self) -> None:
        async with asyncio.timeout(60):
            while True:
                if self.process.poll() is not None:
                    pytest.fail(f"server failed during startup:\n{self.log()}")

                try:
                    answers = await asyncio.gather(
                        self.http.get(f"{self.urls[0]}/health"),
                        self.http.get(f"{self.urls[1]}/health"),
                    )
                    if all(answer.status_code == 200 for answer in answers):
                        return
                except httpx.TransportError:
                    # Uvicorn ещё не принял listener; запуск ограничен общим deadline.
                    await asyncio.sleep(0.02)
                    continue

                await asyncio.sleep(0.02)

    async def wait_log(self, text: str, mark: int) -> None:
        async with asyncio.timeout(30):
            while text not in self.log()[mark:]:
                if self.process.poll() is not None:
                    pytest.fail(f"server exited waiting for {text}:\n{self.log()}")

                await asyncio.sleep(0.02)

    async def reload(self, success: bool = True) -> None:
        mark = len(self.log())
        self.process.send_signal(signal.SIGHUP)
        expected = "MCP reload complete"
        if not success:
            expected = "MCP reload rejected"

        await self.wait_log(expected, mark)

    async def token(self, user: str, role: str, path: str = "/service") -> str:
        public = self.urls[0]
        resource = public + path
        assertion = self._assertions.issue(CLIENT_ID, f"{user}", role, public, resource)
        response = await self.http.post(
            public + "/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "resource": resource,
            },
        )
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])

    def client(
        self, token: str, listener: int = 0, path: str = "/service"
    ) -> Client[Any]:
        transport = StreamableHttpTransport(self.urls[listener] + path, auth=token)
        return Client(transport, timeout=15)

    async def marker(self, path: Path) -> int:
        async with asyncio.timeout(30):
            while True:
                if path.exists():
                    contents = path.read_text()
                    if contents:
                        return int(contents)

                await asyncio.sleep(0.02)

    async def stop(self, received: signal.Signals) -> None:
        self.process.send_signal(received)
        code = await asyncio.to_thread(self.process.wait, 15)
        assert code == 0, self.log()

    async def rejected_start(self, reason: str) -> None:
        """Отказ startup должен завершить процесс и оставить оба порта закрытыми."""
        code = await asyncio.to_thread(self.process.wait, 15)
        output = self.log()
        assert code == 1, output
        assert reason in output
        assert "McpReloadError" in output
        assert "ServerStartError" in output
        assert "Application startup complete" not in output
        assert "MCP reload complete" not in output
        assert "MCP reload rejected; keeping" not in output
        for url in self.urls:
            with pytest.raises(httpx.TransportError):
                await self.http.get(url + "/health")

    async def catalog_is(self, paths: Sequence[str]) -> None:
        for url in self.urls:
            response = await self.http.get(url + "/health")
            assert response.status_code == 200, response.text
            assert sorted(response.json()["endpoints"]) == sorted(paths)

    async def close(self) -> None:
        await self.http.aclose()
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, 10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.to_thread(self.process.wait, 5)
        self._log.close()


@pytest.fixture
async def database(
    pool: AsyncPostgresPool,
    test_postgres: PostgresConfig,
) -> AsyncGenerator[CatalogDatabase, None]:
    schema = "reload_" + uuid4().hex
    await StandMigrations().application(test_postgres, schema, "boba.mcp_server")
    db = CatalogDatabase(pool, schema)
    try:
        await db.seed()
        yield db
    finally:
        await db.execute("drop schema {} cascade")


@pytest.fixture
async def network(
    tmp_path: Path,
    test_postgres: PostgresConfig,
    database: CatalogDatabase,
) -> AsyncGenerator[NetworkServer, None]:
    server = NetworkServer(tmp_path, test_postgres.dbname, database.schema)
    try:
        await server.ready()
        yield server
    finally:
        await server.close()


def text(result: CallToolResult) -> str:
    parts = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)

    return "\n".join(parts)


async def test_two_listeners_users_and_inflight_calls_survive_reload(
    network: NetworkServer,
    database: CatalogDatabase,
) -> None:
    alice = await network.token("alice", "dev")
    bob = await network.token("bob", "weak")
    async with network.client(alice) as first, network.client(bob, 1) as second:
        marker = network.workdir / "long-call.pid"
        release = network.workdir / "long-call.release"
        call = asyncio.create_task(
            first.call_tool_mcp(
                "fake_gate", {"release": str(release), "marker": str(marker)}
            )
        )
        try:
            await network.marker(marker)
            await database.endpoint("/added", ("dev",))
            assert (
                await network.http.post(network.urls[1] + "/added")
            ).status_code == 404
            pid = network.process.pid
            await network.reload()
            assert network.process.pid == pid
            assert not call.done()
            release.touch()
            assert not (await call).is_error
            result = await second.call_tool_mcp("fake_whoami", {})
            assert "bob|weak|/service" in text(result)
            result = await first.call_tool_mcp("fake_whoami", {})
            assert "alice|dev|/service" in text(result)
        finally:
            release.touch()
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)

    added = await network.token("alice", "dev", "/added")
    async with network.client(added, 1, "/added") as client:
        names = [tool.name for tool in await client.list_tools()]
        assert "fake_whoami" in names


async def test_reload_revokes_roles_and_removes_routes_for_existing_tokens(
    network: NetworkServer,
    database: CatalogDatabase,
) -> None:
    alice = await network.token("alice", "dev")
    bob = await network.token("bob", "weak")
    async with network.client(alice) as first, network.client(bob, 1) as second:
        assert not (await second.call_tool_mcp("fake_whoami", {})).is_error
        await database.execute(
            "delete from {0}.grants where tgt_kind_id in "
            "(select id from {0}.roles where role='weak')"
        )
        await network.reload()
        denied = await network.http.post(
            network.urls[1] + "/service", headers={"Authorization": f"Bearer {bob}"}
        )
        assert denied.status_code == 403, denied.text
        assert not (await first.call_tool_mcp("fake_whoami", {})).is_error

    await database.execute("delete from {}.mcp_endpoints where prefix_url='/service'")
    await network.reload()
    for url in network.urls:
        assert (await network.http.post(url + "/service")).status_code == 404


@pytest.mark.parametrize("failure", ["missing_table", "unknown_tool"])
async def test_failed_reload_keeps_serving_and_recovers_after_db_repair(
    network: NetworkServer,
    database: CatalogDatabase,
    failure: str,
) -> None:
    token = await network.token("alice", "dev")
    if failure == "missing_table":
        await database.execute("alter table {}.mcp_endpoints rename to unavailable")
    else:
        await database.execute(
            "update {}.mcp_endpoints set tools=%s where prefix_url='/service'",
            (Jsonb(["nonexistent_export"]),),
        )

    try:
        await network.reload(success=False)
        async with network.client(token, 1) as client:
            assert not (await client.call_tool_mcp("fake_whoami", {})).is_error
    finally:
        if failure == "missing_table":
            await database.execute("alter table {}.unavailable rename to mcp_endpoints")
        else:
            await database.execute(
                "update {}.mcp_endpoints set tools=%s where prefix_url='/service'",
                (Jsonb(["fake_whoami", "fake_sleep", "fake_gate", "fake_echo"]),),
            )

    await network.reload()
    async with network.client(token) as client:
        assert not (await client.call_tool_mcp("fake_whoami", {})).is_error


async def test_running_limit_is_shared_between_listeners_and_reload_generations(
    network: NetworkServer,
) -> None:
    alice = await network.token("alice", "dev")
    bob = await network.token("bob", "weak")
    async with network.client(alice) as first, network.client(bob, 1) as second:
        markers = [network.workdir / "slot1.pid", network.workdir / "slot2.pid"]
        release = network.workdir / "slots.release"
        calls = [
            asyncio.create_task(
                first.call_tool_mcp(
                    "fake_gate", {"release": str(release), "marker": str(markers[0])}
                )
            ),
            asyncio.create_task(
                second.call_tool_mcp(
                    "fake_gate", {"release": str(release), "marker": str(markers[1])}
                )
            ),
        ]
        try:
            for marker in markers:
                await network.marker(marker)

            await network.reload()
            async with network.client(alice, 1) as third:
                refused = await third.call_tool_mcp("fake_whoami", {})
                assert refused.is_error
                assert "already runs 2 calls" in text(refused)

            release.touch()
            results = await asyncio.gather(*calls)
            assert all(not result.is_error for result in results)
            assert not (await first.call_tool_mcp("fake_whoami", {})).is_error
        finally:
            release.touch()
            for call in calls:
                call.cancel()

            await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.parametrize("received", [signal.SIGINT, signal.SIGTERM])
async def test_stop_while_reload_waits_on_sql_lock_closes_both_listeners(
    network: NetworkServer,
    database: CatalogDatabase,
    received: signal.Signals,
) -> None:
    async with database.pool.connection() as connection, connection.transaction():
        statement = sql.SQL("lock table {}.mcp_endpoints in access exclusive mode")
        await connection.execute(statement.format(sql.Identifier(database.schema)))
        mark = len(network.log())
        network.process.send_signal(signal.SIGHUP)
        await network.wait_log("MCP reload: reading", mark)
        # SQL reload заблокирован; запросы обязаны использовать опубликованный кеш.
        for url in network.urls:
            assert (await network.http.get(url + "/health")).status_code == 200

        await network.stop(received)

    assert "MCP reload complete" not in network.log()[mark:]
    assert "Task was destroyed" not in network.log()
    assert "Traceback" not in network.log()
    for url in network.urls:
        with pytest.raises(httpx.TransportError):
            await network.http.get(url + "/health")


@pytest.mark.parametrize("received", [signal.SIGINT, signal.SIGTERM])
async def test_stop_cancels_two_users_calls_and_incomplete_http_request(
    network: NetworkServer,
    received: signal.Signals,
) -> None:
    alice = await network.token("alice", "dev")
    bob = await network.token("bob", "weak")
    async with network.client(alice) as first, network.client(bob, 1) as second:
        markers = [network.workdir / "stop1.pid", network.workdir / "stop2.pid"]
        release = network.workdir / "stop.release"
        calls = [
            asyncio.create_task(
                first.call_tool_mcp(
                    "fake_gate", {"release": str(release), "marker": str(markers[0])}
                )
            ),
            asyncio.create_task(
                second.call_tool_mcp(
                    "fake_gate", {"release": str(release), "marker": str(markers[1])}
                )
            ),
        ]
        try:
            pids = []
            for marker in markers:
                pids.append(await network.marker(marker))

            url = httpx.URL(network.urls[1])
            assert url.port is not None
            reader, writer = await asyncio.open_connection(url.host, url.port)
            try:
                writer.write(b"POST /service HTTP/1.1\r\nHost: localhost\r\n")
                await writer.drain()
                await network.stop(received)
                results = await asyncio.wait_for(asyncio.gather(*calls), 5)
                for result in results:
                    assert result.is_error
                    assert "shutdown" in text(result)

                assert await asyncio.wait_for(reader.read(), 2) == b""
                for pid in pids:
                    with pytest.raises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            release.touch()
            for call in calls:
                call.cancel()

            await asyncio.gather(*calls, return_exceptions=True)

    assert "Task was destroyed" not in network.log()
    assert "Traceback" not in network.log()


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(
            InvalidEndpoint("/token", ("fake_whoami",), "route is reserved"),
            id="reserved-exact",
        ),
        pytest.param(
            InvalidEndpoint("/token/admin", ("fake_whoami",), "route is reserved"),
            id="reserved-child",
        ),
        pytest.param(
            InvalidEndpoint("/invalid", ("missing_export",), "unknown tools"),
            id="unknown-tool",
        ),
        pytest.param(
            InvalidEndpoint("/invalid", (42,), "tools.0"),
            id="invalid-row",
        ),
    ],
)
async def test_invalid_catalog_survives_failed_reload_and_blocks_restart_until_repaired(
    network: NetworkServer,
    database: CatalogDatabase,
    test_postgres: PostgresConfig,
    invalid: InvalidEndpoint,
) -> None:
    """Одна БД и каталог процесса: reload сохраняет кеш, перезапуск его теряет."""
    token = await network.token("bob", "weak")
    await database.endpoint("/added", ("dev",))
    await database.execute(
        "delete from {0}.grants where tgt_kind_id in "
        "(select id from {0}.roles where role='weak')"
    )
    await database.invalidate(invalid)
    snapshot = await database.snapshot()
    original_pid = network.process.pid
    mark = len(network.log())
    await network.reload(success=False)
    assert invalid.reason in network.log()[mark:]
    assert network.process.poll() is None
    assert network.process.pid == original_pid
    assert await database.snapshot() == snapshot
    await network.catalog_is(("/service", "/narrow"))
    async with network.client(token, 1) as client:
        answer = await client.call_tool_mcp("fake_whoami", {})
        assert not answer.is_error
        assert "bob|weak|/service" in text(answer)

    assert (await network.http.post(network.urls[0] + "/added")).status_code == 404
    await network.stop(signal.SIGTERM)
    await network.close()

    # Повторный процесс использует ту же БД, схему и рабочий каталог.
    failed = NetworkServer(network.workdir, test_postgres.dbname, database.schema)
    try:
        await failed.rejected_start(invalid.reason)
        assert await database.snapshot() == snapshot
    finally:
        await failed.close()

    await database.execute(
        "delete from {}.mcp_endpoints where prefix_url=%s", (invalid.path,)
    )
    recovered = NetworkServer(network.workdir, test_postgres.dbname, database.schema)
    try:
        await recovered.ready()
        await recovered.catalog_is(("/service", "/narrow", "/added"))
        allowed = await recovered.token("alice", "dev", "/added")
        async with recovered.client(allowed, 1, "/added") as client:
            assert not (await client.call_tool_mcp("fake_whoami", {})).is_error

        await recovered.stop(signal.SIGTERM)
    finally:
        await recovered.close()


@pytest.mark.parametrize("path", ["/token", "/token/admin"])
async def test_reserved_route_rejection_can_be_retried_without_restart(
    network: NetworkServer,
    database: CatalogDatabase,
    path: str,
) -> None:
    """Повторный отказ не отравляет подписку; исправленный каталог принимается."""
    invalid = InvalidEndpoint(path, ("fake_whoami",), "route is reserved")
    await database.endpoint("/added", ("dev",))
    await database.invalidate(invalid)
    snapshot = await database.snapshot()
    pid = network.process.pid
    for _ in range(2):
        mark = len(network.log())
        await network.reload(success=False)
        assert f"endpoint {path!r}: route is reserved" in network.log()[mark:]
        assert "MCP reload complete" not in network.log()[mark:]
        assert await database.snapshot() == snapshot
        await network.catalog_is(("/service", "/narrow"))

    await database.execute("delete from {}.mcp_endpoints where prefix_url=%s", (path,))
    await network.reload()
    assert network.process.poll() is None
    assert network.process.pid == pid
    await network.catalog_is(("/service", "/narrow", "/added"))
    token = await network.token("alice", "dev", "/added")
    async with network.client(token, 1, "/added") as client:
        assert not (await client.call_tool_mcp("fake_whoami", {})).is_error
