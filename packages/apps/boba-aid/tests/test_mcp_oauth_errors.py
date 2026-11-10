"""Границы OAuth переводят отказы в HTTP и не скрывают отмену задачи."""

import asyncio
import secrets
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx2
import pytest
from aid_stand import StandTokens
from fastapi import FastAPI

from boba.aid.auth.claims import SessionClaims
from boba.aid.mcp_servers.oauth import (
    McpBrowserOAuth,
    McpClientMetadataDocument,
    McpOAuthCallback,
    McpOAuthClientDocument,
    McpOAuthError,
)
from boba.aid.mcp_servers.oauth_store import McpOAuthStore
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """HTTP-маршрутам OAuth не нужен контекст WebSocket Chainlit."""


class BrokenTokenReader(StandTokens):
    """Нарушает контракт TokenReader для проверки последней границы HTTP."""

    def read(self, token: str) -> SessionClaims:
        raise OSError("session key provider is unavailable")


class CancelledTokenReader(StandTokens):
    """Проверяет отмену HTTP-запроса во время чтения сессии."""

    def read(self, token: str) -> SessionClaims:
        raise asyncio.CancelledError


class BrokenMetadata(McpBrowserOAuth):
    """Нарушает контракт источника документа для проверки HTTP-адаптера."""

    def client_document(self) -> McpClientMetadataDocument:
        raise RuntimeError("metadata renderer failed")


@pytest.fixture
async def broker(pool: AsyncPostgresPool) -> McpBrowserOAuth:
    store = McpOAuthStore(pool, "unused_oauth", SecretCipher(secrets.token_bytes(32)))
    return McpBrowserOAuth(
        store, "http://localhost/aid/", client_name="Callback errors"
    )


@pytest.fixture
async def client(broker: McpBrowserOAuth) -> AsyncIterator[httpx2.AsyncClient]:
    app = FastAPI()
    callback = McpOAuthCallback(broker, StandTokens(), "session")
    app.add_api_route(callback.ROUTE, callback.serve)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.mark.parametrize("authenticated", [False, True])
async def test_absent_or_invalid_session_is_401(
    client: httpx2.AsyncClient, authenticated: bool
) -> None:
    headers = {}
    if authenticated:
        headers["Authorization"] = "Bearer invalid-session"

    response = await client.get(McpOAuthCallback.ROUTE, headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "query", ["", "?code=code", "?state=unknown", "?code=code&state=unknown"]
)
async def test_callback_input_or_state_refusal_is_400(
    client: httpx2.AsyncClient, query: str
) -> None:
    token = StandTokens.issue(StandTokens.session("reader", uuid4()))
    response = await client.get(
        McpOAuthCallback.ROUTE + query,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "MCP OAuth" in response.text


async def test_unexpected_session_failure_is_500_with_domain_cause(
    broker: McpBrowserOAuth, caplog: pytest.LogCaptureFixture
) -> None:
    app = FastAPI()
    callback = McpOAuthCallback(broker, BrokenTokenReader(), "session")
    app.add_api_route(callback.ROUTE, callback.serve)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get(
            callback.ROUTE, headers={"Authorization": "Bearer test-session"}
        )

    assert response.status_code == 500
    assert "internal AID error" in response.text
    assert "session key provider is unavailable" not in response.text
    assert "session key provider is unavailable" in caplog.text
    failures = [record for record in caplog.records if record.exc_info is not None]
    assert len(failures) == 1
    details = failures[0].exc_info
    assert details is not None
    assert isinstance(details[1], McpOAuthError)
    assert isinstance(details[1].__cause__, OSError)


async def test_callback_cancellation_is_not_a_500(broker: McpBrowserOAuth) -> None:
    app = FastAPI()
    callback = McpOAuthCallback(broker, CancelledTokenReader(), "session")
    app.add_api_route(callback.ROUTE, callback.serve)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        with pytest.raises(asyncio.CancelledError):
            await http.get(
                callback.ROUTE, headers={"Authorization": "Bearer test-session"}
            )


async def test_metadata_failure_is_500(
    pool: AsyncPostgresPool, caplog: pytest.LogCaptureFixture
) -> None:
    store = McpOAuthStore(pool, "unused_oauth", SecretCipher(secrets.token_bytes(32)))
    broker = BrokenMetadata(
        store, "http://localhost/aid/", client_name="Metadata errors"
    )
    route = McpOAuthClientDocument(broker)
    app = FastAPI()
    app.add_api_route(route.ROUTE, route.serve)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get(route.ROUTE)

    assert response.status_code == 500
    assert "internal AID error" in response.text
    assert "metadata renderer failed" not in response.text
    assert "metadata renderer failed" in caplog.text


async def test_invalid_configuration_has_oauth_boundary(
    pool: AsyncPostgresPool,
) -> None:
    store = McpOAuthStore(pool, "unused_oauth", SecretCipher(secrets.token_bytes(32)))
    with pytest.raises(McpOAuthError) as failed:
        McpBrowserOAuth(store, "not-an-application-url", client_name="Invalid URL")

    assert failed.value.__cause__ is not None
    assert "Configuring MCP OAuth" in str(failed.value)
