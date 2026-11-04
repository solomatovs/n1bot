"""Контракт локального патча Chainlit: настоящие MCP-транспорты и их lifecycle."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from acp_stand import AppsHost
from chainlit.config import config
from chainlit.context import init_http_context
from chainlit.mcp import McpConnection
from chainlit.server import connect_mcp, disconnect_mcp
from chainlit.session import WebsocketSession
from chainlit.types import (
    ConnectMCPRequest,
    ConnectSseMCPRequest,
    ConnectStdioMCPRequest,
    ConnectStreamableHttpMCPRequest,
    DisconnectMCPRequest,
)
from chainlit.user import User
from fastapi import HTTPException
from mcp import ClientSession
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from boba.mcp_client.client import HttpEndpoint, McpServerConfig, SseEndpoint

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def chainlit_context() -> AsyncIterator[None]:
    """Сессия теста не должна остаться в контексте следующего сценария."""
    init_http_context()
    yield
    init_http_context()


class ObservedRequest(BaseModel):
    method: str
    authorization: str


@dataclass(frozen=True)
class Endpoint:
    payload: ConnectMCPRequest
    request_log: Path

    def requests(self) -> list[ObservedRequest]:
        observed: list[ObservedRequest] = []
        for line in self.request_log.read_text().splitlines():
            observed.append(ObservedRequest.model_validate_json(line))

        return observed


@pytest.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WebsocketSession]:
    monkeypatch.setattr(config.features.mcp, "enabled", True)
    monkeypatch.setattr(config.features.mcp.sse, "enabled", True)
    monkeypatch.setattr(config.features.mcp.streamable_http, "enabled", True)
    monkeypatch.setattr(config.features.mcp.stdio, "enabled", True)
    monkeypatch.setattr(
        config.features.mcp.stdio,
        "allowed_executables",
        [Path(sys.executable).name],
    )

    def emit(event: str, data: Any) -> None:
        return None

    async def emit_call(
        event: Literal["ask", "call_fn"], data: Any, timeout: int | None
    ) -> None:
        return None

    result = WebsocketSession(
        id=str(uuid4()),
        socket_id=str(uuid4()),
        emit=emit,
        emit_call=emit_call,
        user_env={},
        client_type="webapp",
        user=User(identifier="owner"),
    )
    try:
        yield result
    finally:
        await result.delete()


@pytest.fixture(params=["streamable-http", "sse", "stdio"])
def endpoint(
    request: pytest.FixtureRequest, session: WebsocketSession, tmp_path: Path
) -> Iterator[Endpoint]:
    transport = request.param
    script = Path(__file__).with_name("chainlit_mcp_server.py")
    request_log = tmp_path / "requests.jsonl"
    request_log.write_text("")
    if transport == "stdio":
        command = f"{sys.executable} {script}"
        payload = ConnectStdioMCPRequest(
            sessionId=session.id,
            clientType="stdio",
            name="fixture",
            fullCommand=command,
        )
        yield Endpoint(payload, request_log)
        return

    host = AppsHost(["--request-log", str(request_log)], transport, script=script)
    try:
        host.await_listening()
        endpoint = McpServerConfig.model_validate(host.plain()).endpoint
        assert isinstance(endpoint, (HttpEndpoint, SseEndpoint))
        if transport == "sse":
            payload = ConnectSseMCPRequest(
                sessionId=session.id,
                clientType="sse",
                name="fixture",
                url=endpoint.url(),
                headers={"Authorization": "Bearer patch-fixture"},
            )
        else:
            payload = ConnectStreamableHttpMCPRequest(
                sessionId=session.id,
                clientType="streamable-http",
                name="fixture",
                url=endpoint.url(),
                headers={"Authorization": "Bearer patch-fixture"},
            )
        yield Endpoint(payload, request_log)
    finally:
        host.stop()


async def test_connect_call_reconnect_disconnect(
    session: WebsocketSession, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = endpoint.payload
    callbacks: list[tuple[str, ...]] = []

    async def connected(connection: McpConnection, client: ClientSession) -> None:
        tools = await client.list_tools()
        callbacks.append(("connect", connection.name, tools.tools[0].name))

    async def disconnected(name: str, client: ClientSession) -> None:
        await client.list_tools()
        callbacks.append(("disconnect", name))

    monkeypatch.setattr(config.code, "on_mcp_connect", connected)
    monkeypatch.setattr(config.code, "on_mcp_disconnect", disconnected)

    response = await connect_mcp(payload, session.user)
    assert response.status_code == 200, response.body
    assert json.loads(bytes(response.body))["mcp"]["tools"] == [{"name": "echo"}]
    first = session.mcp_sessions["fixture"]
    reply = await first.client.call_tool("echo", {"text": "Привет, MCP"})
    assert isinstance(reply, CallToolResult)
    assert isinstance(reply.content[0], TextContent)
    assert reply.content[0].text == "Привет, MCP"

    response = await connect_mcp(payload, session.user)
    assert response.status_code == 200, response.body
    assert first.task.done()
    second = session.mcp_sessions["fixture"]
    assert second is not first

    response = await disconnect_mcp(
        DisconnectMCPRequest(sessionId=session.id, name="fixture"), session.user
    )
    assert response.status_code == 200
    assert second.task.done()
    assert session.mcp_sessions == {}
    assert callbacks == [
        ("connect", "fixture", "echo"),
        ("disconnect", "fixture"),
        ("connect", "fixture", "echo"),
        ("disconnect", "fixture"),
    ]
    observed = endpoint.requests()
    for received in observed:
        assert received.authorization == "Bearer patch-fixture"
    if payload.clientType == "streamable-http":
        assert sum(received.method == "DELETE" for received in observed) == 2


async def test_callback_failure_closes_connection(
    session: WebsocketSession, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = endpoint.payload

    async def rejected(connection: McpConnection, client: ClientSession) -> None:
        await client.list_tools()
        raise RuntimeError("fixture callback rejected")

    monkeypatch.setattr(config.code, "on_mcp_connect", rejected)
    response = await connect_mcp(payload, session.user)
    assert response.status_code == 400
    assert "fixture callback rejected" in json.loads(bytes(response.body))["detail"]
    assert session.mcp_sessions == {}
    assert not any(t.get_name() == "mcp-session-fixture" for t in asyncio.all_tasks())
    if payload.clientType == "streamable-http":
        assert any(received.method == "DELETE" for received in endpoint.requests())


async def test_disabled_and_wrong_user(
    session: WebsocketSession, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = endpoint.payload
    with pytest.raises(HTTPException) as unauthorized:
        await connect_mcp(payload, User(identifier="stranger"))
    assert unauthorized.value.status_code == 401

    monkeypatch.setattr(config.features.mcp, "enabled", False)
    with pytest.raises(HTTPException) as disabled:
        await connect_mcp(payload, session.user)
    assert disabled.value.status_code == 400
    assert disabled.value.detail == "This app does not support MCP."
    assert session.mcp_sessions == {}


async def test_session_delete_closes_connection(
    session: WebsocketSession, endpoint: Endpoint
) -> None:
    payload = endpoint.payload
    response = await connect_mcp(payload, session.user)
    assert response.status_code == 200, response.body
    connection = session.mcp_sessions["fixture"]
    await session.delete()
    assert connection.task.done()
    assert session.mcp_sessions == {}
