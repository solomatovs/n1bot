"""Browser authorization-code OAuth for a user-owned remote MCP server.

Errors:
McpAppError — the requested authorization has no valid pending browser flow.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx2
from fastapi import Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from pydantic import AnyUrl

from boba.aid.auth.claims import TokenReader, TokenRejectedError
from boba.aid.auth.http import RequestTokens
from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.oauth_store import (
    McpOAuthStore,
    McpOAuthStoreError,
    OAuthWriteGate,
    UserOAuthTokens,
)
from boba.mcp_client.client import BrowserOAuthRuntime


@dataclass
class PendingOAuth:
    """One short-lived browser redirect; never persisted with credentials."""

    server_id: UUID
    user_id: UUID
    login: str
    state: str
    started: float
    url: asyncio.Future[str]
    callback: asyncio.Future[AuthorizationCodeResult]
    completed: asyncio.Event
    failure: str = ""
    task: asyncio.Task[None] | None = None


class OAuthLimit(IntEnum):
    REDIRECT_SEC = 60
    CALLBACK_SEC = 300
    EXCHANGE_SEC = 30


class OAuthParam(StrEnum):
    CODE = "code"
    STATE = "state"
    ISS = "iss"
    ERROR = "error"


class McpClientMetadataDocument(OAuthClientMetadata):
    """Public RFC 7591 metadata document for MCP Client ID Metadata Documents."""

    client_id: AnyUrl


class McpBrowserOAuth(BrowserOAuthRuntime):
    """SDK provider factory and browser redirect coordinator for one AID process."""

    def __init__(self, store: McpOAuthStore, redirect_uri: str) -> None:
        self._store = store
        self._redirect_uri = redirect_uri
        self._client_metadata_url = redirect_uri.removesuffix("/callback") + "/client"
        self._pending: dict[tuple[UUID, UUID], PendingOAuth] = {}
        self._states: dict[str, PendingOAuth] = {}
        self._gates: dict[tuple[UUID, UUID], OAuthWriteGate] = {}
        self._editing: set[UUID] = set()

    def provider(
        self,
        server_id: UUID,
        user_id: UUID,
        resource: str,
        *,
        interactive: bool = False,
    ) -> httpx2.Auth:
        if server_id in self._editing:
            raise McpAppError("MCP OAuth server is being edited")
        key = (server_id, user_id)

        async def redirect(url: str) -> None:
            if not interactive:
                raise McpAppError("MCP server requires browser OAuth sign-in")
            pending = self._pending.get(key)
            if pending is None:
                raise McpAppError("MCP server requires browser OAuth sign-in")
            values = parse_qs(urlsplit(url).query)
            state = values.get(OAuthParam.STATE, [""])[0]
            if not state or state in self._states:
                raise McpAppError("MCP OAuth authorization URL has no unique state")
            pending.state = state
            self._states[state] = pending
            pending.url.set_result(url)

        async def callback() -> AuthorizationCodeResult:
            pending = self._pending.get(key)
            if pending is None:
                raise McpAppError("MCP OAuth browser authorization expired")
            return await asyncio.wait_for(
                pending.callback, timeout=OAuthLimit.CALLBACK_SEC
            )

        metadata = self._client_metadata()
        metadata_url: str | None = None
        if self._client_metadata_url.startswith("https://"):
            metadata_url = self._client_metadata_url
        gate = self._gates.setdefault(key, OAuthWriteGate())
        return OAuthClientProvider(
            server_url=resource,
            client_metadata=metadata,
            storage=UserOAuthTokens(self._store, server_id, user_id, gate),
            redirect_handler=redirect,
            callback_handler=callback,
            client_metadata_url=metadata_url,
        )

    def client_document(self) -> McpClientMetadataDocument:
        """Public metadata read by an authorization server that supports CIMD."""
        return McpClientMetadataDocument(
            **self._client_metadata().model_dump(),
            client_id=AnyUrl(self._client_metadata_url),
        )

    def _client_metadata(self) -> OAuthClientMetadata:
        return OAuthClientMetadata(
            client_name="Boba AID MCP Client",
            redirect_uris=[AnyUrl(self._redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — OAuth method name
            application_type="web",
        )

    async def begin(
        self, server_id: UUID, user_id: UUID, login: str, resource: str
    ) -> str:
        if server_id in self._editing:
            raise McpAppError("MCP OAuth server is being edited")
        key = (server_id, user_id)
        if key in self._pending:
            raise McpAppError("MCP OAuth sign-in is already in progress")
        loop = asyncio.get_running_loop()
        pending = PendingOAuth(
            server_id=server_id,
            user_id=user_id,
            login=login,
            state="",
            started=monotonic(),
            url=loop.create_future(),
            callback=loop.create_future(),
            completed=asyncio.Event(),
        )
        self._pending[key] = pending
        pending.task = asyncio.create_task(self._probe(pending, resource))
        try:
            return await asyncio.wait_for(pending.url, timeout=OAuthLimit.REDIRECT_SEC)
        except TimeoutError as exc:
            self._forget(pending)
            raise McpAppError(
                "MCP server did not offer browser OAuth sign-in within 60 seconds"
            ) from exc
        except BaseException:
            self._forget(pending)
            raise

    async def disconnect(self, server_id: UUID, user_id: UUID) -> None:
        if server_id in self._editing:
            raise McpAppError("MCP OAuth server is being edited")
        key = (server_id, user_id)
        if key in self._pending:
            raise McpAppError("MCP OAuth sign-in is still in progress")
        gate = self._gates.setdefault(key, OAuthWriteGate())
        try:
            async with gate.lock:
                await self._store.delete(server_id, user_id)
                gate.generation += 1
        except McpOAuthStoreError as exc:
            raise McpAppError("MCP OAuth credentials could not be removed") from exc

    @asynccontextmanager
    async def editing(self, server_id: UUID) -> AsyncGenerator[None, None]:
        """Stop old token writers while a server row and its tokens change."""
        if server_id in self._editing:
            raise McpAppError("MCP OAuth server is already being edited")
        self._editing.add(server_id)
        for pending in tuple(self._pending.values()):
            if pending.server_id == server_id:
                self._forget(pending)
        gates = [
            gate
            for (owned_server, _), gate in self._gates.items()
            if owned_server == server_id
        ]
        acquired: list[OAuthWriteGate] = []
        try:
            for gate in gates:
                await gate.lock.acquire()
                acquired.append(gate)
                gate.generation += 1
            yield
        finally:
            for gate in acquired:
                gate.lock.release()
            self._editing.remove(server_id)

    async def finish(self, login: str, code: str, state: str, iss: str | None) -> None:
        pending = self._states.get(state)
        if pending is None:
            raise McpAppError("MCP OAuth state is missing or expired")
        if monotonic() - pending.started > OAuthLimit.CALLBACK_SEC:
            self._forget(pending)
            raise McpAppError("MCP OAuth state is missing or expired")
        if pending.login != login:
            raise McpAppError("MCP OAuth callback belongs to another AID user")
        if pending.callback.done():
            raise McpAppError("MCP OAuth authorization code was already used")
        pending.callback.set_result(
            AuthorizationCodeResult(code=code, state=state, iss=iss)
        )
        try:
            await asyncio.wait_for(
                pending.completed.wait(), timeout=OAuthLimit.EXCHANGE_SEC
            )
        except TimeoutError as exc:
            self._forget(pending)
            raise McpAppError("MCP OAuth token exchange timed out") from exc
        if pending.failure:
            raise McpAppError(pending.failure)

    def reject(self, login: str, state: str) -> None:
        pending = self._states.get(state)
        if pending is None or pending.login != login:
            raise McpAppError("MCP OAuth refusal has no matching AID sign-in")
        self._forget(pending)

    async def _probe(self, pending: PendingOAuth, resource: str) -> None:
        try:
            auth = self.provider(
                pending.server_id, pending.user_id, resource, interactive=True
            )
            # A valid MCP request lets the SDK perform PRM/AS discovery, DCR,
            # PKCE and token exchange. The response body is not needed here.
            async with (
                httpx2.AsyncClient(auth=auth, timeout=310) as http,
                http.stream(
                    "POST",
                    resource,
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "boba-aid", "version": "1"},
                        },
                    },
                ),
            ):
                pass
            if not pending.url.done():
                pending.failure = "MCP server did not require browser OAuth"
                pending.url.set_exception(McpAppError(pending.failure))
        except Exception as exc:
            pending.failure = f"MCP OAuth sign-in failed: {type(exc).__name__}"
            if not pending.url.done():
                pending.url.set_exception(McpAppError(pending.failure))
        finally:
            pending.completed.set()
            self._forget(pending)

    def _forget(self, pending: PendingOAuth) -> None:
        if not pending.url.done():
            pending.url.set_exception(McpAppError("MCP OAuth sign-in was cancelled"))
        key = (pending.server_id, pending.user_id)
        if self._pending.get(key) is pending:
            self._pending.pop(key)
        if pending.state and self._states.get(pending.state) is pending:
            self._states.pop(pending.state)
        task = pending.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


class McpOAuthCallback:
    """Authenticated AID browser return for the SDK authorization-code flow."""

    ROUTE = "/api/mcp/oauth/callback"

    def __init__(
        self,
        oauth: McpBrowserOAuth,
        tokens: TokenReader,
        cookie: str,
        chat_url: str,
    ) -> None:
        self._oauth = oauth
        self._tokens = tokens
        self._request_tokens = RequestTokens(cookie)
        self._chat_url = chat_url

    async def serve(self, request: Request) -> Response:
        token = self._request_tokens.of_request(request)
        if token is None:
            return PlainTextResponse(
                "Sign in to AID before connecting MCP", status_code=401
            )
        try:
            login = self._tokens.read(token).identifier
        except TokenRejectedError:
            return PlainTextResponse("AID sign-in expired", status_code=401)
        try:
            await self._accept(login, request)
        except McpAppError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return RedirectResponse(self._chat_url, status_code=303)

    async def _accept(self, login: str, request: Request) -> None:
        params = request.query_params
        state = params.get(OAuthParam.STATE)
        if not state:
            raise McpAppError("MCP OAuth callback lacks state")
        if params.get(OAuthParam.ERROR):
            self._oauth.reject(login, state)
            raise McpAppError("MCP OAuth sign-in was denied")
        code = params.get(OAuthParam.CODE)
        if not code:
            raise McpAppError("MCP OAuth callback lacks code")
        await self._oauth.finish(login, code, state, params.get(OAuthParam.ISS))


class McpOAuthClientDocument:
    """Public HTTP route for the client metadata URL advertised to the SDK."""

    ROUTE = "/api/mcp/oauth/client"

    def __init__(self, oauth: McpBrowserOAuth) -> None:
        self._oauth = oauth

    async def serve(self) -> JSONResponse:
        document = self._oauth.client_document()
        return JSONResponse(document.model_dump(mode="json", exclude_none=True))
