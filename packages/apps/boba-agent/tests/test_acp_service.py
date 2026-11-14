"""Настоящий WebSocket-вход службы с PostgreSQL и локальным HTTP-инференсом."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import time
from uuid import uuid4

import pytest
import uvicorn
from acp.connection import Connection
from acp.ws.client import create_websocket_stream
from conftest import InferenceServer
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import TokenVerifier
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import WebSocketRoute
from websockets.exceptions import InvalidStatus

from boba.agent.acp_server import AgentProfiles
from boba.agent.serve import AcpEndpoint, CallerTokens, ServeAgents, ServeRouter
from boba.agent.settings import AgentProfile, ServeHistory, ServeSection
from boba.db.postgres.connection import PostgresConfig
from boba.oauth.claims import TokenClaims

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


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


class AgentService:
    """Штатный ACP-сервис с отдельным источником токенов для тестов."""

    def __init__(
        self, postgres: PostgresConfig, inference: InferenceServer, port: int
    ) -> None:
        self.url = f"ws://127.0.0.1:{port}/acp"
        self.tokens = FixtureTokens()
        self.inference = inference
        profile = inference.profiles.default(frozenset({"test"})).config
        profiles: dict[str, AgentProfile] = {"test": profile}
        section = ServeSection(profiles=profiles)
        history = ServeHistory(schema="boba", postgres=postgres)
        self.agents = ServeAgents(section, history, AgentProfiles(profiles), "test", ())
        self.endpoint = AcpEndpoint(ServeRouter(self.agents), CallerTokens(), 30)
        backend = BearerAuthBackend(self.tokens)
        app = Starlette(
            routes=[WebSocketRoute("/acp", self.endpoint.websocket)],
            middleware=[Middleware(AuthenticationMiddleware, backend=backend)],
        )
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
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


@pytest.fixture
async def service(
    postgres: PostgresConfig,
    inference: InferenceServer,
    free_tcp_port_factory: Callable[[], int],
    agent_timeout: float,
) -> AsyncIterator[AgentService]:
    instance = AgentService(postgres, inference, free_tcp_port_factory())
    await instance.agents.start()
    worker = asyncio.create_task(instance.server.serve())
    try:
        async with asyncio.timeout(agent_timeout):
            while not instance.server.started:
                if worker.done():
                    await worker
                    pytest.fail("ACP server stopped during startup")

                await asyncio.sleep(0.01)

        yield instance

    finally:
        await instance.endpoint.close()
        instance.server.should_exit = True
        async with asyncio.timeout(agent_timeout):
            await worker

        store = instance.agents.hub.store
        async with store.pool.connection() as connection:
            await connection.execute(
                "delete from boba.records where session_id in "
                "(select session_id from boba.sessions where owner_id = %s)",
                (str(instance.tokens.user_id),),
            )
            await connection.execute(
                "delete from boba.sessions where owner_id = %s",
                (str(instance.tokens.user_id),),
            )

        await instance.agents.stop()


async def test_websocket_rejects_missing_token(service: AgentService) -> None:
    with pytest.raises(InvalidStatus) as failure:
        await create_websocket_stream(service.url)

    assert failure.value.response.status_code == 403


async def test_websocket_negotiates_v2_and_creates_session(
    service: AgentService,
) -> None:
    async with service.connection() as connection:
        initialized = await connection.send_request(
            "initialize", {"protocolVersion": 2}
        )
        assert initialized["protocolVersion"] == 2
        assert initialized["capabilities"]["session"]["systemPrompt"] == {}
        created = await connection.send_request("session/new", {"cwd": "/workspace"})
        assert created["availableCommands"]
        assert created["configOptions"]
        owner = str(service.tokens.user_id)
        session = await service.agents.hub.store.session(created["sessionId"], owner)
        assert session["owner_id"] == owner
