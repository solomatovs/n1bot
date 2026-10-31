"""Приложение MCP-серверов чата настоящим клиентом MCP: McpDirectory над
McpServers с серверами профиля — обычный сервер по HTTP, тот же сервер за
способом входа BearerAuth с неверным токеном и закрытый порт.

Проверяется: список отдаёт серверы профиля без chat в порядке профиля;
состояние сервера — connected с числом инструментов, needs_auth с короткой
причиной, failed с короткой причиной; карточка отдаёт инструменты с
пометками read-only и destructive; переподключение создаёт новое
подключение; имя не из набора зрителя — отказ McpAppError.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest
from acp_stand import REPO_ROOT, StandPorts
from pydantic import SecretStr

from boba.aid.agent.relay import ChatTools
from boba.aid.mcp_servers.directory import (
    McpAppError,
    McpDirectory,
    McpOrigin,
    McpState,
    McpToolRow,
    McpViewer,
)
from boba.chat.profiles import SelectedProfile
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    HttpLocation,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpServersConfig,
    StdioCommand,
)
from boba.stand.signin import SignInStand

pytestmark = pytest.mark.anyio

HTTP_SERVER = REPO_ROOT / "packages/services/boba-mcp-client/tests/standard_server.py"
HTTP = "http"
NEEDS = "needs"
GONE = "gone"
DENIED = "--denied"
"""Флаг сервера, которому нужен вход: отвечает 401 на каждый POST."""
LOGIN = "ivanov"
ROLES = frozenset({"dev"})


def _config(endpoint: HttpEndpoint | StdioCommand) -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        limits=McpServerLimits(connect_timeout_sec=20.0, call_timeout_sec=60.0),
    )


def _location(port: int) -> HttpLocation:
    return HttpLocation(scheme="http", host="127.0.0.1", port=port, path="")


def _endpoint(port: int) -> HttpEndpoint:
    return HttpEndpoint(service=_location(port), path="/mcp")


def _profile(names: list[str]) -> SelectedProfile:
    config = SignInStand.profile().model_copy(update={"mcp": list(names)})

    return SelectedProfile(name=SignInStand.PROFILE, config=config)


def viewer_of(names: list[str]) -> McpViewer:
    return McpViewer(login=LOGIN, roles=ROLES, profile=_profile(names))


@pytest.fixture
async def servers() -> AsyncIterator[McpServers]:
    """Клиент MCP над тремя серверами профиля: http, needs и gone."""
    http_port = StandPorts().free()
    needs_port = StandPorts().free()
    gone_port = StandPorts().free()
    http = subprocess.Popen(
        [sys.executable, str(HTTP_SERVER), "streamable-http", str(http_port)]
    )
    needs = subprocess.Popen(
        [sys.executable, str(HTTP_SERVER), "streamable-http", str(needs_port), DENIED]
    )
    try:
        for port in (http_port, needs_port):
            await _wait_listening(port)
    finally:
        if http.poll() is not None or needs.poll() is not None:
            http.terminate()
            needs.terminate()

    with socket.socket() as gone_probe:
        gone_probe.bind(("127.0.0.1", gone_port))

    needs_endpoint = HttpEndpoint(
        service=_location(needs_port),
        path="/mcp",
        auth=BearerAuth(token=SecretStr("wrong-token")),
    )
    servers = McpServers(
        McpServersConfig(
            servers={
                HTTP: _config(_endpoint(http_port)),
                NEEDS: _config(needs_endpoint),
                GONE: _config(_endpoint(gone_port)),
            }
        )
    )
    try:
        yield servers
    finally:
        await servers.stop()
        http.terminate()
        http.wait(timeout=10)
        needs.terminate()
        needs.wait(timeout=10)


async def _wait_listening(port: int) -> None:
    """Сервер слушает порт."""
    for _ in range(100):
        try:
            await asyncio.to_thread(socket.create_connection, ("127.0.0.1", port), 1.0)
        except OSError:
            await asyncio.sleep(0.2)
            continue

        return

    msg = f"the server on port {port} never started listening"
    raise AssertionError(msg)


class TestMcpApp:
    async def test_listed_shows_profile_servers_without_chat(
        self, servers: McpServers
    ) -> None:
        """Список: серверы профиля в порядке профиля, chat не показывается."""
        viewer = viewer_of([ChatTools.NAME, HTTP, NEEDS, GONE])
        directory = McpDirectory(servers)
        listed = await directory.listed(viewer)

        names = [row.name for row in listed.servers]
        assert names == [HTTP, NEEDS, GONE], names
        for row in listed.servers:
            assert row.origin is McpOrigin.PROFILE, row
            assert not row.editable, row
            assert row.transport == "streamable-http", row
            assert row.address, row

    async def test_status_reports_each_server_state(self, servers: McpServers) -> None:
        """Состояния: connected с числом инструментов, needs_auth с короткой
        причиной, failed с короткой причиной."""
        viewer = viewer_of([HTTP, NEEDS, GONE])
        directory = McpDirectory(servers)

        connected = await directory.status(viewer, HTTP)
        assert connected.state is McpState.CONNECTED, connected
        assert connected.tools > 0, connected
        assert connected.issue == "", connected

        needs = await directory.status(viewer, NEEDS)
        assert needs.state is McpState.NEEDS_AUTH, needs
        assert needs.issue, needs

        gone = await directory.status(viewer, GONE)
        assert gone.state is McpState.FAILED, gone
        assert gone.issue, gone

    async def test_detail_shows_the_tools_with_their_hints(
        self, servers: McpServers
    ) -> None:
        """Карточка сервера: строка списка (адрес и транспорт), инструменты с
        пометками read-only и destructive; у сервера не на связи инструментов
        нет."""
        viewer = viewer_of([HTTP, GONE])
        directory = McpDirectory(servers)

        detail = await directory.detail(viewer, HTTP)
        assert detail.state is McpState.CONNECTED, detail
        assert detail.row.name == HTTP, detail
        assert detail.row.transport == "streamable-http", detail
        assert detail.row.address, detail
        names = [tool.name for tool in detail.tools]
        assert "add" in names, detail
        for tool in detail.tools:
            assert isinstance(tool, McpToolRow), tool

        gone = await directory.detail(viewer, GONE)
        assert gone.state is McpState.FAILED, gone
        assert gone.row.address, gone
        assert gone.issue, gone
        assert gone.tools == (), gone

    async def test_reconnect_opens_a_fresh_connection(
        self, servers: McpServers
    ) -> None:
        """Переподключение закрывает порт и состояние берётся заново."""
        viewer = viewer_of([HTTP])
        directory = McpDirectory(servers)

        before = await directory.status(viewer, HTTP)
        assert before.state is McpState.CONNECTED, before

        after = await directory.reconnect(viewer, HTTP)
        assert after.state is McpState.CONNECTED, after
        assert after.tools == before.tools, (before, after)

    async def test_name_outside_the_viewer_is_an_error(
        self, servers: McpServers
    ) -> None:
        """Имя не из набора зрителя — отказ McpAppError."""
        viewer = viewer_of([HTTP])
        directory = McpDirectory(servers)
        with pytest.raises(McpAppError, match="not among the profile servers"):
            await directory.status(viewer, "missing")

        with pytest.raises(McpAppError, match="not among the profile servers"):
            await directory.reconnect(viewer, "missing")
