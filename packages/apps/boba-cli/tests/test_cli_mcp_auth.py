"""Серверы MCP и их вход из файла настроек агента: ключ `mcpServers` слоя с
секцией `auth` (local, proxy, bearer); секреты входа держатся правилами файла
с секретами; `boba acp` сам получает токен сервиса и обновляет его посреди
работы."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from acp import RequestError, text_block
from test_cli_acp import (
    AcpProcess,
    FakeModel,
    RecordingClient,
    call,
    handshake,
    result_text,
)
from test_cli_settings import ENV, settings, write, write_private

from boba.cli.acp_server import StopReasonName
from boba.cli.settings import (
    Arguments,
    McpClientJson,
    McpServerBuilder,
    SettingsError,
)
from boba.mcp_client.client import BearerAuth, HttpEndpoint, LocalAuth, ProxyAuth
from boba.stand.service_signin import FakeServiceProcess

pytestmark = pytest.mark.anyio

WORLD_READABLE = 0o644
SERVER = "boba"
WHOAMI = "mcp__boba__fake_whoami"
Document = dict[str, Any]


class AuthEntries:
    """Секции `auth` записи сервера стенда в файле настроек агента."""

    def __init__(self, service: FakeServiceProcess) -> None:
        self._service = service

    def local(self) -> Document:
        return {
            "auth": "local",
            "login": FakeServiceProcess.LOGIN,
            "password": FakeServiceProcess.PASSWORD,
        }

    def proxy(self) -> Document:
        return {
            "auth": "proxy",
            "issuer": {
                "scheme": "http",
                "host": FakeServiceProcess.HOST,
                "port": self._service.port,
                "path": "/",
            },
            "clientId": FakeServiceProcess.CLIENT_ID,
            "clientSecret": FakeServiceProcess.CLIENT_SECRET,
            "secret": FakeServiceProcess.PROXY_SECRET,
            "login": "ivanov",
            "roles": ["dev"],
        }

    def servers(self, auth: Document) -> Document:
        """Ключ `mcpServers` с сервером стенда и названным входом."""
        return {SERVER: {"type": "http", "url": self._service.url(), "auth": auth}}


@pytest.fixture
async def boba(model: FakeModel, tmp_path: Path) -> AsyncIterator[AcpProcess]:
    process = AcpProcess(tmp_path, model)
    try:
        yield process
    finally:
        await process.kill()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[FakeServiceProcess]:
    started = FakeServiceProcess(tmp_path / "service", 300)
    try:
        started.wait()
        yield started
    finally:
        started.stop()


@pytest.fixture
def brief_service(tmp_path: Path) -> Iterator[FakeServiceProcess]:
    """Сервис стенда с токеном доступа на две секунды."""
    started = FakeServiceProcess(tmp_path / "service", 2)
    try:
        started.wait()
        yield started
    finally:
        started.stop()


def layer_document(servers: Document) -> Document:
    return {"mcpServers": servers}


LOCAL_SERVERS: Document = {
    SERVER: {
        "type": "http",
        "url": "http://127.0.0.1:9/mcp/general",
        "auth": {"auth": "local", "login": "agent", "password": "agent-pw"},
    }
}

PROXY_SERVERS: Document = {
    SERVER: {
        "type": "http",
        "url": "http://127.0.0.1:9/mcp/general",
        "auth": {
            "auth": "proxy",
            "issuer": {"scheme": "http", "host": "127.0.0.1", "port": 9, "path": "/"},
            "clientId": "chat",
            "clientSecret": "client-file-secret",
            "secret": "proxy-file-secret",
            "login": "ivanov",
            "roles": ["ADM"],
        },
    }
}


class TestServersOfTheSettingsFile:
    async def test_server_with_auth_becomes_a_client_config(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "settings.json"
        write_private(path, layer_document(LOCAL_SERVERS))

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        builder = McpServerBuilder(McpClientJson(), {})
        config = builder.config(SERVER, effective.mcp_servers[SERVER])
        assert isinstance(config.endpoint, HttpEndpoint)
        auth = config.endpoint.auth
        assert isinstance(auth, LocalAuth)
        assert auth.login == "agent"
        assert auth.password.get_secret_value() == "agent-pw"

    async def test_proxy_entry_carries_its_login_and_roles(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "settings.json"
        write_private(path, layer_document(PROXY_SERVERS))

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        auth = effective.mcp_servers[SERVER].auth
        assert isinstance(auth, ProxyAuth)
        assert auth.login == "ivanov"
        assert auth.roles == ("ADM",)
        assert auth.client_id == "chat"

    async def test_settings_server_wins_over_the_project_file(
        self, tmp_path: Path
    ) -> None:
        write(
            tmp_path / "project" / ".mcp.json",
            {
                "mcpServers": {
                    SERVER: {"type": "http", "url": "http://127.0.0.1:9/other"},
                    "side": {"type": "http", "url": "http://127.0.0.1:9/side"},
                }
            },
        )
        path = tmp_path / "settings.json"
        write_private(path, layer_document(LOCAL_SERVERS))

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        assert sorted(effective.mcp_servers) == [SERVER, "side"]
        assert effective.mcp_servers[SERVER].url == "http://127.0.0.1:9/mcp/general"

    async def test_upper_layer_with_another_auth_replaces_the_section(
        self, tmp_path: Path
    ) -> None:
        lower = tmp_path / "lower.json"
        write_private(lower, layer_document(PROXY_SERVERS))
        upper = tmp_path / "upper.json"
        bearer = {SERVER: {"auth": {"auth": "bearer", "token": "key-1"}}}
        write_private(upper, layer_document(bearer))

        arguments = Arguments(settings=(str(lower), str(upper)))
        effective = settings(tmp_path, arguments).effective()

        auth = effective.mcp_servers[SERVER].auth
        assert isinstance(auth, BearerAuth)
        assert auth.token.get_secret_value() == "key-1"
        assert effective.mcp_servers[SERVER].url == "http://127.0.0.1:9/mcp/general"

    async def test_auth_together_with_headers_is_refused(self, tmp_path: Path) -> None:
        servers = json.loads(json.dumps(LOCAL_SERVERS))
        servers[SERVER]["headers"] = {"Authorization": "Bearer ready"}
        path = tmp_path / "settings.json"
        write_private(path, layer_document(servers))
        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        with pytest.raises(SettingsError, match="both 'auth' and 'headers'"):
            McpServerBuilder(McpClientJson(), {}).config(
                SERVER, effective.mcp_servers[SERVER]
            )

    async def test_server_name_that_breaks_tool_names_is_refused(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "settings.json"
        write_private(path, layer_document({"bad__name": LOCAL_SERVERS[SERVER]}))

        with pytest.raises(SettingsError, match="mcpServers"):
            settings(tmp_path, Arguments(settings=(str(path),))).effective()


class TestSecretsOfTheAuthSection:
    async def test_world_readable_file_with_a_password_is_refused(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "settings.json"
        write(path, layer_document(LOCAL_SERVERS))
        path.chmod(WORLD_READABLE)

        with pytest.raises(SettingsError) as failure:
            settings(tmp_path, Arguments(settings=(str(path),))).effective()

        text = str(failure.value)
        assert "0644" in text
        assert "mcpServers.boba.auth.password" in text
        assert "agent-pw" not in text

    async def test_world_readable_file_with_proxy_secrets_is_refused(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "settings.json"
        write(path, layer_document(PROXY_SERVERS))
        path.chmod(WORLD_READABLE)

        with pytest.raises(SettingsError) as failure:
            settings(tmp_path, Arguments(settings=(str(path),))).effective()

        text = str(failure.value)
        assert "mcpServers.boba.auth.clientSecret" in text
        assert "mcpServers.boba.auth.secret" in text

    async def test_auth_secrets_on_the_command_line_are_refused(
        self, tmp_path: Path
    ) -> None:
        for servers in (LOCAL_SERVERS, PROXY_SERVERS):
            arguments = Arguments(settings=(json.dumps(layer_document(servers)),))

            with pytest.raises(SettingsError, match="command line"):
                settings(tmp_path, arguments).effective()

    async def test_public_settings_mask_the_auth_secrets(self, tmp_path: Path) -> None:
        local = tmp_path / "local.json"
        write_private(local, layer_document(LOCAL_SERVERS))
        proxy = tmp_path / "proxy.json"
        write_private(proxy, layer_document({"second": PROXY_SERVERS[SERVER]}))

        arguments = Arguments(settings=(str(local), str(proxy)))
        public = json.dumps(settings(tmp_path, arguments).effective().public())

        assert "agent-pw" not in public
        assert "client-file-secret" not in public
        assert "proxy-file-secret" not in public
        assert '"login": "ivanov"' in public

    async def test_server_without_secrets_may_live_in_an_open_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "settings.json"
        write(path, layer_document({SERVER: {"type": "http", "url": "http://h/mcp"}}))
        path.chmod(WORLD_READABLE)

        effective = settings(tmp_path, Arguments(settings=(str(path),))).effective()

        assert effective.mcp_servers[SERVER].auth is None
        assert ENV


class AgentTurns:
    """Ходы `boba acp` с вызовом инструмента сервера стенда."""

    def __init__(self, boba: AcpProcess, client: RecordingClient) -> None:
        self._boba = boba
        self._client = client

    def document(self, servers: Document) -> Document:
        document = self._boba.settings_document("bypassPermissions", True)
        document["mcpServers"] = servers

        return document

    async def whoami(self, conn: Any, session_id: str) -> str:
        """Ход с вызовом fake_whoami; итог — текст результата инструмента."""
        seen = len(self._client.starts())
        response = await conn.prompt(
            session_id=session_id,
            prompt=[text_block(call({"name": WHOAMI, "arguments": {}}))],
        )
        assert response.stop_reason == StopReasonName.END_TURN.value
        starts = self._client.starts()
        assert len(starts) == seen + 1, self._boba.stderr[-20:]
        done = self._client.progress_of(starts[-1].tool_call_id)[-1]
        assert done.status == "completed", (result_text(done), self._boba.stderr[-20:])

        return result_text(done)


class TestAgentSignsInItself:
    async def test_local_account_of_the_file_runs_the_tool(
        self, boba: AcpProcess, service: FakeServiceProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        turns = AgentTurns(boba, client)
        entries = AuthEntries(service)
        conn = await boba.start(
            client, turns.document(entries.servers(entries.local()))
        )
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])

            answer = await turns.whoami(conn, session.session_id)

            pid = boba.process.pid
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert answer.startswith("carol|dev|"), answer
        password = FakeServiceProcess.PASSWORD.encode()
        assert password not in cmdline
        assert password not in environ
        assert FakeServiceProcess.PASSWORD not in "\n".join(boba.stderr)
        assert FakeServiceProcess.PASSWORD not in "\n".join(boba.raw_stdout)

    async def test_proxy_entry_of_the_file_runs_the_tool_as_its_login(
        self, boba: AcpProcess, service: FakeServiceProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        turns = AgentTurns(boba, client)
        entries = AuthEntries(service)
        conn = await boba.start(
            client, turns.document(entries.servers(entries.proxy()))
        )
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])

            answer = await turns.whoami(conn, session.session_id)

            pid = boba.process.pid
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert answer.startswith("ivanov|dev|"), answer
        for secret in (
            FakeServiceProcess.CLIENT_SECRET,
            FakeServiceProcess.PROXY_SECRET,
        ):
            assert secret.encode() not in cmdline
            assert secret.encode() not in environ
            assert secret not in "\n".join(boba.stderr)

    async def test_local_token_expired_between_turns_is_renewed(
        self, boba: AcpProcess, brief_service: FakeServiceProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        turns = AgentTurns(boba, client)
        entries = AuthEntries(brief_service)
        conn = await boba.start(
            client, turns.document(entries.servers(entries.local()))
        )
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            first = await turns.whoami(conn, session.session_id)

            await asyncio.sleep(brief_service.token_ttl_sec + 1.5)
            second = await turns.whoami(conn, session.session_id)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert first.startswith("carol|dev|"), first
        assert second.startswith("carol|dev|"), second

    async def test_proxy_token_expired_between_turns_is_exchanged_again(
        self, boba: AcpProcess, brief_service: FakeServiceProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        turns = AgentTurns(boba, client)
        entries = AuthEntries(brief_service)
        conn = await boba.start(
            client, turns.document(entries.servers(entries.proxy()))
        )
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            first = await turns.whoami(conn, session.session_id)

            await asyncio.sleep(brief_service.token_ttl_sec + 1.5)
            second = await turns.whoami(conn, session.session_id)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert first.startswith("ivanov|dev|"), first
        assert second.startswith("ivanov|dev|"), second

    async def test_wrong_password_refuses_the_session_with_the_reason(
        self, boba: AcpProcess, service: FakeServiceProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        turns = AgentTurns(boba, client)
        entries = AuthEntries(service)
        local = entries.local()
        local["password"] = "not-the-password"
        conn = await boba.start(client, turns.document(entries.servers(local)))
        try:
            await handshake(conn)

            with pytest.raises(RequestError) as refused:
                await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        finally:
            await conn.close()
            await boba.close_stdin()

        details = json.dumps(refused.value.data)
        assert "HTTP 401" in details, details
        assert "mcp auth local" in details, details
        assert "not-the-password" not in details

    async def test_world_readable_file_with_auth_secrets_stops_the_agent(
        self, boba: AcpProcess, service: FakeServiceProcess
    ) -> None:
        turns = AgentTurns(boba, RecordingClient())
        entries = AuthEntries(service)
        document = turns.document(entries.servers(entries.local()))
        # секрет провайдера убран: отказ должен назвать именно секрет входа
        del document["models"]["fake"]["provider"]["apiKey"]
        path = boba.write_settings(document)
        path.chmod(WORLD_READABLE)

        await boba.spawn()
        code = await boba.wait()

        errors = "\n".join(boba.stderr)
        assert code != 0
        assert "mcpServers.boba.auth.password" in errors, errors
        assert "0644" in errors, errors
        assert FakeServiceProcess.PASSWORD not in errors
