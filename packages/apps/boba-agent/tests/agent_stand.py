"""Общий стенд: PostgreSQL, инференс, MCP и жизненный цикл службы; без моделей ACP."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from time import time
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from acp import RequestError
from acp.connection import Connection
from acp.ws.client import create_websocket_stream
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import TokenVerifier
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute

from boba.agent import models as m
from boba.agent.acp_server import AgentProfiles, SessionHub
from boba.agent.history import HistoryStore
from boba.agent.llm import OpenAiTransport, ProviderConfiguration, Providers
from boba.agent.serve import AcpEndpoint, ServeAgents
from boba.agent.settings import (
    AgentProfile,
    McpSettings,
    ModelSettings,
    ProviderSettings,
    SettingsLayer,
)
from boba.agent.tools import McpBinding
from boba.crypto import SecretCipher
from boba.db.postgres.connection import PostgresConfig
from boba.llm.http.openai import WireChunk
from boba.oauth.claims import TokenClaims
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import NoneAuth


class InferenceServer:
    """Локальный HTTP-сервер с заданным ответом и записью полученного тела."""

    def __init__(self, port: int, timeout: float) -> None:
        self.requests: list[bytes] = []
        self.request_headers: list[Mapping[str, str]] = []
        self.request_paths: list[str] = []
        self.response = Response(status_code=501)
        self.responses: tuple[Response, ...] = ()
        self.settings = ProviderSettings(
            base_url=f"http://127.0.0.1:{port}/v1",
            auth=NoneAuth(method="none"),
            retry_statuses={503: 3},
            retry_backoff_sec=0,
            timeout_sec=max(120, timeout),
            transport=HttpTransportConfig(stream_stall_sec=timeout),
        )
        self.profiles = AgentProfiles(
            {
                "test": AgentProfile(
                    roles=["*"],
                    model=ModelSettings(model="test"),
                )
            }
        )
        app = Starlette(
            routes=[
                Route("/v1/chat/completions", self.handle, methods=["POST"]),
                Route("/client/v1/chat/completions", self.handle, methods=["POST"]),
            ]
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="error", ws="none"
            )
        )

    async def handle(self, request: Request) -> Response:
        self.requests.append(await request.body())
        self.request_headers.append(dict(request.headers))
        self.request_paths.append(request.url.path)

        if self.responses:
            index = len(self.requests) - 1
            if index >= len(self.responses):
                return Response("Unexpected inference request", status_code=500)

            return self.responses[index]

        return self.response


class AgentSystem:
    """Стенд запускает настоящий SessionHub с PostgreSQL и локальным HTTP-инференсом."""

    def __init__(
        self,
        history: HistoryStore,
        inference: InferenceServer,
        postgres: PostgresConfig,
        timeout: float,
    ) -> None:
        self.history = history
        self.inference = inference
        self.postgres = postgres
        self.timeout = timeout
        self.owner = uuid4().hex
        self.owners = {self.owner}
        self.profiles = AgentProfiles(
            {
                "hidden": AgentProfile(
                    roles=["administrators"],
                    model=ModelSettings(model="hidden-model"),
                ),
                "test": AgentProfile(
                    roles=["users"],
                    model=ModelSettings(
                        model="system-test-model",
                        sampling={"temperature": 0.2},
                        inference={"max_completion_tokens": 128},
                        provider_parameters={"parallel_tool_calls": True},
                        system_prompt="You are a test assistant.",
                    ),
                ),
                "alternative": AgentProfile(
                    roles=["operators"],
                    model=ModelSettings(model="alternative-model"),
                ),
                "same-model": AgentProfile(
                    roles=["variants"],
                    model=ModelSettings(
                        model="system-test-model", sampling={"temperature": 0.8}
                    ),
                ),
            }
        )
        self.llm = OpenAiTransport()
        self.providers = Providers(history, SecretCipher(secrets.token_bytes(32)))
        self.settings = SettingsLayer(
            mcp=McpSettings(list_timeout_sec=timeout, call_timeout_sec=timeout)
        )
        self.hub = SessionHub(
            history,
            self.llm,
            self.profiles,
            self.settings,
            "test",
            providers=self.providers,
        )
        self.connections = AsyncExitStack()

    async def seed_provider(self) -> None:
        configuration = ProviderConfiguration(
            owner_id=self.owner,
            provider_id="test",
            settings=self.inference.settings,
            headers={},
        )
        await self.providers.save(configuration, enabled=True)

    async def restart(self) -> None:
        await self.connections.aclose()
        await self.hub.close()
        self.hub = SessionHub(
            self.history,
            self.llm,
            self.profiles,
            self.settings,
            "test",
            providers=self.providers,
        )
        await self.hub.start(self.postgres)

    async def idle(self, session_id: str) -> None:
        async with asyncio.timeout(self.timeout):
            await self.hub.agents[session_id].idle.wait()

        assert self.hub.agents[session_id].last_error is None

    async def assert_replay(self, session_id: str) -> None:
        rows = (await self.history.read(session_id)).rows
        boundaries = tuple(
            row for row in rows if isinstance(row, m.LlmRequestBoundaryRecord)
        )
        assert len(boundaries) == len(self.inference.requests)
        for boundary, sent in zip(boundaries, self.inference.requests, strict=True):
            by_id = await self.history.openai(session_id, boundary.record_id)
            by_seq = await self.history.openai(session_id, through_seq=boundary.seq)
            assert by_id.request == json.loads(sent)
            assert by_seq.request == by_id.request

    def answer(self, text: str) -> Response:
        return self.response({"content": text}, "stop")

    def call(self, call_id: str, name: str, arguments: Mapping[str, Any]) -> Response:
        return self.response(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            },
            "tool_calls",
        )

    def response(self, delta: Mapping[str, Any], finish: str) -> Response:
        chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        usage = {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        body = (
            f"data: {json.dumps(chunk)}\n\n"
            f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n"
        )

        return Response(body, media_type="text/event-stream")

    async def close(self) -> None:
        await self.connections.aclose()

        await self.hub.close()
        async with self.history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.providers where owner_id = any(%s)",
                (list(self.owners),),
            )
            await connection.execute(
                "delete from boba.records where session_id in "
                "(select session_id from boba.sessions where owner_id = any(%s))",
                (list(self.owners),),
            )
            await connection.execute(
                "delete from boba.sessions where owner_id = any(%s)",
                (list(self.owners),),
            )


class McpServer:
    """MCP-сервер стенда; обслуживает обе редакции MCP-over-ACP без моделей ACP."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.callbacks: dict[str, dict[str, Any]] = {}
        self.callback_results: dict[str, Any] = {}
        self.tool_calls: list[dict[str, Any]] = []
        self.binding = McpBinding.REQUEST
        self.methods: list[str] = []
        self.initialized = False
        self.connect_error = -32601
        self.versions = ["2026-07-28"]
        self.protocol_version = "2025-11-25"
        self.connect_ready = asyncio.Event()
        self.connect_ready.set()
        self.tool_started = asyncio.Event()
        self.release_tool = asyncio.Event()
        self.release_tool.set()

    async def receive(self, method: str, params: Any, is_notification: bool) -> Any:
        if method == "mcp/connect":
            self.methods.append(method)
            await self.connect_ready.wait()
            if self.binding is McpBinding.REQUEST:
                raise RequestError(self.connect_error, "MCP connect rejected")

            return {"connectionId": "workspace-connection"}

        if method == "mcp/disconnect":
            self.methods.append(method)
            assert params["connectionId"] == "workspace-connection"
            return {}

        assert method == "mcp/message", f"Unexpected client operation: {method}"
        self.methods.append(params["method"])
        if self.binding is McpBinding.CONNECTION:
            assert params["connectionId"] == "workspace-connection"
            assert "serverId" not in params
            if params["method"] == "server/discover":
                assert self.protocol_version == "2026-07-28"
                self.initialized = True
                return {
                    "supportedVersions": self.versions,
                    "capabilities": {"tools": {}},
                }

            if params["method"] == "initialize":
                assert params["params"]["capabilities"] == {
                    "roots": {},
                    "elicitation": {"form": {}},
                }
                if self.protocol_version == "2026-07-28":
                    raise RequestError(
                        -32022,
                        "Use MCP 2026-07-28",
                        {"requested": "2025-11-25", "supported": self.versions},
                    )

                return {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test", "version": "1"},
                }

            if params["method"] == "notifications/initialized":
                assert is_notification
                self.initialized = True
                return None

            assert self.initialized
            result = await self.mcp_call(params)
            return result["result"]

        assert not is_notification
        assert params["serverId"] == "workspace"
        assert params["requestId"]
        assert (
            params["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
            == "2026-07-28"
        )
        if params["method"] == "server/discover":
            return {
                "result": {
                    "supportedVersions": self.versions,
                    "capabilities": {"tools": {}},
                }
            }

        return await self.mcp_call(params)

    async def mcp_call(self, params: dict) -> dict:
        if params["method"] == "tools/list":
            return {
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo text in the user workspace",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ]
                }
            }

        assert params["method"] == "tools/call"
        self.tool_calls.append(params["params"])
        self.tool_started.set()
        await self.release_tool.wait()
        for method, values in self.callbacks.items():
            self.callback_results[method] = await self.connection.send_request(
                "mcp/message",
                {
                    "connectionId": "workspace-connection",
                    "method": method,
                    "params": values,
                },
            )

        return {"result": {"content": [{"type": "text", "text": "Tool result 世界"}]}}


class FixtureTokens(TokenVerifier):
    """Проверяет токены стенда через штатный BearerAuthBackend."""

    def __init__(self) -> None:
        self.user_id = uuid4()
        now = int(time())
        claims = TokenClaims.model_validate(
            {
                "version": 2,
                "kind": "access",
                "iss": "boba-acp-test",
                "aud": "boba-acp-test",
                "sub": self.user_id,
                "client_id": "acp-test",
                "scope": "test",
                "identity": {"login": "acp-test"},
                "credential": {"kind": "none", "code": "not_delegated"},
                "generation": "test",
                "iat": now,
                "exp": now + 3600,
                "roles": [],
            }
        )
        self.token = AccessToken(
            token=uuid4().hex,
            client_id="acp-test",
            scopes=["test"],
            expires_at=claims.exp,
            claims=claims.model_dump(mode="json"),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self.token.token:
            return None

        return self.token


class ServiceHost:
    """WebSocket-хост для заданного ACP endpoint; запуск, авторизация и очистка БД."""

    def __init__(
        self,
        agents: ServeAgents,
        endpoint: AcpEndpoint,
        tokens: FixtureTokens,
        port: int,
    ) -> None:
        self.url = f"ws://127.0.0.1:{port}/acp"
        self.agents = agents
        self.endpoint = endpoint
        self.tokens = tokens
        app = Starlette(
            routes=[WebSocketRoute("/acp", endpoint.websocket)],
            middleware=[
                Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(tokens))
            ],
        )
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Connection, None]:
        stream = await create_websocket_stream(
            self.url, headers={"Authorization": f"Bearer {self.tokens.token.token}"}
        )
        connection = Connection(self.receive, stream)
        try:
            yield connection
        finally:
            await connection.close()

    async def receive(self, method: str, params: object, is_notification: bool) -> None:
        assert is_notification
        assert method == "session/update"

    @asynccontextmanager
    async def running(self, timeout: float) -> AsyncGenerator[ServiceHost, None]:
        await self.agents.start()
        worker = asyncio.create_task(self.server.serve())
        try:
            async with asyncio.timeout(timeout):
                while not self.server.started:
                    if worker.done():
                        await worker
                        pytest.fail("ACP server stopped during startup")
                    await asyncio.sleep(0.01)
            yield self
        finally:
            await self.endpoint.close()
            self.server.should_exit = True
            async with asyncio.timeout(timeout):
                await worker
            store = self.agents.hub.store
            async with store.pool.connection() as connection:
                await connection.execute(
                    "delete from boba.providers where owner_id = %s",
                    (str(self.tokens.user_id),),
                )
                await connection.execute(
                    "delete from boba.records where session_id in "
                    "(select session_id from boba.sessions where owner_id = %s)",
                    (str(self.tokens.user_id),),
                )
                await connection.execute(
                    "delete from boba.sessions where owner_id = %s",
                    (str(self.tokens.user_id),),
                )
            await self.agents.stop()


class Llm:
    def __init__(self):
        self.requests = []

    def for_user(self, owner, providers):
        return self

    async def generate(self, provider, request, output, emit):
        self.requests.append(request)
        output.status = 200
        output.address = "https://test.invalid/v1/chat/completions"
        if len(self.requests) == 1:
            message = {
                "tool_calls": [
                    {
                        "id": "test-call",
                        "type": "function",
                        "function": {"name": "test", "arguments": "{}"},
                    }
                ]
            }
        else:
            message = {"content": "Готово\r\n世界"}
        for delta in output.response.deltas(
            WireChunk.model_validate(
                {
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 2},
                }
            )
        ):
            await emit(delta.position, delta.kind, delta.text)
