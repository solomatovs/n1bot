"""Браузерный OAuth для пользовательского MCP-сервера и его HTTP-маршруты.

Ошибки:
McpAppError — вход отклонён, просрочен или нарушено состояние попытки.
McpOAuthError — подкласс McpAppError: сбой хранилища, SDK или координатора.
McpAuthRequired — фоновому запросу MCP нужен браузерный вход пользователя.
ValidationError — прямое создание DTO McpClientMetadataDocument с неверными полями.
CancelledError — отмена асинхронной операции распространяется без упаковки.

HTTP-маршруты переводят отказы сессии в 401, отказы входа в 400,
McpOAuthError в 500 с записью причины в журнал. Контекст editing не меняет
ошибки тела вызывающего кода; собственные вход и выход имеют контракт выше.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from posixpath import normpath
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

import httpx2
from fastapi import Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.utils import is_valid_client_metadata_url
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
from pydantic import AnyHttpUrl, AnyUrl

from boba.aid.auth.claims import TokenReader, TokenRejectedError
from boba.aid.auth.http import RequestTokens
from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.oauth_store import (
    McpOAuthStore,
    OAuthWriteGate,
    UserOAuthTokens,
)
from boba.identity.errors import HttpErrorMessage
from boba.mcp_client.client import (
    BrowserOAuthAuth,
    BrowserOAuthRuntime,
    HttpEndpoint,
    McpAuthRequired,
    McpClientError,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpToolServer,
    McpUnansweredError,
    SseEndpoint,
)


class McpOAuthError(McpAppError):
    """Сбой инфраструктуры OAuth, отличимый от отказа пользователя или сервера."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            user_message=(
                "The MCP OAuth operation failed because of an internal AID error. "
                "Details are in the AID log."
            ),
        )

    def http_message(self) -> HttpErrorMessage:
        return HttpErrorMessage(status_code=500, content=self.view_message().content)


class OAuthErrors:
    """Граница зависимостей OAuth для координатора, SDK и HTTP-адаптеров."""

    @contextmanager
    def boundary(self, action: str) -> Iterator[None]:
        try:
            yield
        except (McpAppError, McpAuthRequired):
            raise
        except Exception as exc:
            raise McpOAuthError(f"{action} failed: {exc}") from exc

    def internal(self, error: BaseException) -> bool:
        """Находит сбой OAuth под транспортными обёртками MCP и anyio."""
        if isinstance(error, McpOAuthError):
            return True

        if isinstance(error, BaseExceptionGroup):
            return any(map(self.internal, error.exceptions))

        if error.__cause__ is not None:
            return self.internal(error.__cause__)

        return False


@dataclass
class PendingOAuth:
    """One short-lived browser redirect; never persisted with credentials."""

    server_id: UUID
    user_id: UUID
    login: str
    return_url: str
    state: str
    started: float
    url: asyncio.Future[str]
    callback: asyncio.Future[AuthorizationCodeResult]
    completed: asyncio.Event
    failure: McpAppError | None = None
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


class OAuthReturnUrls:
    """Проверяет адрес возврата в пределах AID до начала авторизации.

    McpBrowserOAuth создаёт проверку из public_url и сохраняет принятый
    адрес в состоянии конкретной попытки входа.
    """

    def __init__(self, app_url: AnyHttpUrl) -> None:
        self._errors = OAuthErrors()
        self._origin = (app_url.scheme, app_url.host, app_url.port)
        path = app_url.path
        if path is None:
            path = "/"

        self._prefix = path.rstrip("/")

    def checked(self, url: AnyHttpUrl) -> str:
        with self._errors.boundary("Checking OAuth return URL"):
            origin = (url.scheme, url.host, url.port)
            if origin != self._origin:
                raise McpAppError(
                    "OAuth return URL must have the AID application's origin"
                )

            if url.username is not None:
                raise McpAppError("OAuth return URL must not contain user information")

            if url.password is not None:
                raise McpAppError("OAuth return URL must not contain user information")

            path = url.path
            if path is None:
                path = "/"

            if not self._inside(path):
                raise McpAppError(
                    "OAuth return URL must stay within the AID URL prefix"
                )

            normalized = normpath(unquote(path).replace("\\", "/"))
            if not self._inside(normalized):
                raise McpAppError(
                    "OAuth return URL must stay within the AID URL prefix"
                )

            return str(url)

    def _inside(self, path: str) -> bool:
        if path == self._prefix:
            return True

        return path.startswith(self._prefix + "/")


class BrowserOAuthRequests(httpx2.Auth):
    """Допускает фоновый запрос только после входа пользователя.

    McpBrowserOAuth создаёт обёртку над SDK и личным хранилищем токенов.
    Регистрацию, обновление токена и обмен кодом выполняет SDK; без токенов
    начать этот обмен может только явное действие входа в браузере.
    """

    def __init__(
        self,
        provider: OAuthClientProvider,
        storage: TokenStorage,
        resource: str,
        *,
        interactive: bool,
    ) -> None:
        self._provider = provider
        self._storage = storage
        self._resource = resource
        self._interactive = interactive
        self._errors = OAuthErrors()

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        with self._errors.boundary(f"Authorizing MCP resource {self._resource!r}"):
            if not self._interactive:
                tokens = await self._storage.get_tokens()

                if tokens is None:
                    raise McpAuthRequired(
                        f"MCP resource {self._resource!r} "
                        "requires browser OAuth sign-in",
                        "Sign in with OAuth to connect this MCP server",
                    )

            async with aclosing(self._provider.async_auth_flow(request)) as flow:
                outgoing = await anext(flow)
                while True:
                    response = yield outgoing
                    try:
                        outgoing = await flow.asend(response)
                    except StopAsyncIteration:
                        return


class OAuthConnections(BrowserOAuthRuntime):
    """Создаёт временные порты MCP с браузерным входом для McpBrowserOAuth.

    Порт согласует протокол штатным клиентом AID; фабрика владеет его
    временем жизни и передаёт OAuth-провайдеру интерактивный режим.
    """

    def __init__(self, oauth: McpBrowserOAuth) -> None:
        self._oauth = oauth
        self._errors = OAuthErrors()
        self._limits = McpServerLimits(
            connect_timeout_sec=OAuthLimit.CALLBACK_SEC + OAuthLimit.REDIRECT_SEC,
            call_timeout_sec=OAuthLimit.EXCHANGE_SEC,
        )

    def provider(self, server_id: UUID, user_id: UUID, resource: str) -> httpx2.Auth:
        return self._oauth.provider(server_id, user_id, resource, interactive=True)

    async def connect(
        self, pending: PendingOAuth, endpoint: HttpEndpoint | SseEndpoint
    ) -> None:
        try:
            auth = BrowserOAuthAuth().bound(self, pending.server_id, pending.user_id)
            configured = endpoint.model_copy(update={"auth": auth})
            config = McpServerConfig(endpoint=configured, limits=self._limits)
            port = McpToolServer("oauth", config)
            try:
                await port.described(McpServers.OWN)
            finally:
                await port.close()
        except McpAppError:
            raise
        except McpClientError as exc:
            if self._errors.internal(exc):
                raise McpOAuthError(
                    f"MCP OAuth sign-in at {endpoint.url()} failed: {exc}"
                ) from exc

            explanation = "the MCP connection could not be established"
            if isinstance(exc, McpUnansweredError):
                explanation = exc.brief

            raise McpAppError(
                f"MCP OAuth sign-in at {endpoint.url()} failed: {exc}",
                user_message=(
                    f"Could not connect to MCP server {endpoint.url()}: {explanation}."
                ),
            ) from exc
        except Exception as exc:
            raise McpOAuthError(
                f"Connecting OAuth MCP resource {endpoint.url()} failed: {exc}"
            ) from exc


class McpBrowserOAuth(BrowserOAuthRuntime):
    """SDK provider factory and browser redirect coordinator for one AID process."""

    def __init__(self, store: McpOAuthStore, app_url: str, *, client_name: str) -> None:
        self._errors = OAuthErrors()
        with self._errors.boundary(f"Configuring MCP OAuth for AID {app_url!r}"):
            self._store = store
            self._connections = OAuthConnections(self)
            app = AnyHttpUrl(app_url)
            self._returns = OAuthReturnUrls(app)
            base = str(app).rstrip("/")
            self._redirect_uri = base + McpOAuthCallback.ROUTE
            self._client_name = client_name
            self._client_metadata_url = base + McpOAuthClientDocument.ROUTE
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
        with self._errors.boundary(
            f"Creating OAuth provider for MCP server {server_id}, user {user_id}"
        ):
            if server_id in self._editing:
                raise McpAppError("MCP OAuth server is being edited")
            key = (server_id, user_id)

            async def redirect(url: str) -> None:
                self._redirect(key, resource, url, interactive=interactive)

            async def callback() -> AuthorizationCodeResult:
                pending = self._pending.get(key)
                if pending is None:
                    raise McpAppError("MCP OAuth browser authorization expired")
                try:
                    return await asyncio.wait_for(
                        pending.callback, timeout=OAuthLimit.CALLBACK_SEC
                    )
                except TimeoutError as exc:
                    raise McpAppError(
                        f"Waiting for OAuth browser callback for MCP {resource!r} "
                        f"exceeded {OAuthLimit.CALLBACK_SEC} seconds: {exc}"
                    ) from exc

            metadata = self._client_metadata()
            metadata_url: str | None = None
            # Требования CIMD проверяет SDK; иначе он использует регистрацию клиента.
            if is_valid_client_metadata_url(self._client_metadata_url):
                metadata_url = self._client_metadata_url
            gate = self._gates.setdefault(key, OAuthWriteGate())
            storage = UserOAuthTokens(self._store, server_id, user_id, gate)
            provider = OAuthClientProvider(
                server_url=resource,
                client_metadata=metadata,
                storage=storage,
                redirect_handler=redirect,
                callback_handler=callback,
                client_metadata_url=metadata_url,
            )
            return BrowserOAuthRequests(
                provider, storage, resource, interactive=interactive
            )

    def _redirect(
        self, key: tuple[UUID, UUID], resource: str, url: str, *, interactive: bool
    ) -> None:
        """Публикует один переход в браузер, не заменяя состояние начатого входа."""
        if not interactive:
            raise McpAuthRequired(
                f"MCP resource {resource!r} requires browser OAuth sign-in",
                "Sign in with OAuth to connect this MCP server",
            )

        pending = self._pending.get(key)
        if pending is None:
            raise McpAppError("MCP server requires browser OAuth sign-in")

        if pending.url.done():
            raise McpAppError(
                f"MCP resource {resource!r} requested OAuth authorization again "
                "after browser sign-in had already started; check the "
                "endpoint's allowed roles and the token's granted scopes"
            )

        values = parse_qs(urlsplit(url).query)
        state = values.get(OAuthParam.STATE, [""])[0]
        if not state:
            raise McpAppError("MCP OAuth authorization URL has no state")

        if state in self._states:
            raise McpAppError("MCP OAuth authorization URL has no unique state")

        pending.state = state
        self._states[state] = pending
        pending.url.set_result(url)

    def client_document(self) -> McpClientMetadataDocument:
        """Public metadata read by an authorization server that supports CIMD."""
        with self._errors.boundary("Creating MCP OAuth client metadata"):
            return McpClientMetadataDocument(
                **self._client_metadata().model_dump(),
                client_id=AnyUrl(self._client_metadata_url),
            )

    def _client_metadata(self) -> OAuthClientMetadata:
        return OAuthClientMetadata(
            client_name=self._client_name,
            redirect_uris=[AnyUrl(self._redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — OAuth method name
            application_type="web",
        )

    async def begin(
        self,
        server_id: UUID,
        user_id: UUID,
        login: str,
        endpoint: HttpEndpoint | SseEndpoint,
        *,
        return_url: AnyHttpUrl,
    ) -> str:
        with self._errors.boundary(
            f"Starting OAuth for MCP server {server_id}, user {user_id}"
        ):
            destination = self._returns.checked(return_url)

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
                return_url=destination,
                state="",
                started=monotonic(),
                url=loop.create_future(),
                callback=loop.create_future(),
                completed=asyncio.Event(),
            )
            self._pending[key] = pending
            pending.task = asyncio.create_task(self._probe(pending, endpoint))
            try:
                return await asyncio.wait_for(
                    pending.url, timeout=OAuthLimit.REDIRECT_SEC
                )
            except TimeoutError as exc:
                self._forget(pending)
                raise McpAppError(
                    "MCP server did not offer browser OAuth sign-in within 60 seconds"
                ) from exc
            except BaseException:
                self._forget(pending)
                raise

    async def disconnect(self, server_id: UUID, user_id: UUID) -> None:
        with self._errors.boundary(
            f"Disconnecting OAuth for MCP server {server_id}, user {user_id}"
        ):
            if server_id in self._editing:
                raise McpAppError("MCP OAuth server is being edited")
            key = (server_id, user_id)
            if key in self._pending:
                raise McpAppError("MCP OAuth sign-in is still in progress")
            gate = self._gates.setdefault(key, OAuthWriteGate())
            async with gate.lock:
                await self._store.delete(server_id, user_id)
                gate.generation += 1

    @asynccontextmanager
    async def editing(self, server_id: UUID) -> AsyncGenerator[None, None]:
        """Приостанавливает запись токенов; ошибки тела принадлежат вызывающему."""
        with self._errors.boundary(f"Starting edit of OAuth MCP server {server_id}"):
            if server_id in self._editing:
                raise McpAppError("MCP OAuth server is already being edited")

            self._editing.add(server_id)

        acquired: list[OAuthWriteGate] = []
        try:
            with self._errors.boundary(f"Locking OAuth MCP server {server_id}"):
                for pending in tuple(self._pending.values()):
                    if pending.server_id == server_id:
                        self._forget(pending)

                gates = [
                    gate
                    for (owned_server, _), gate in self._gates.items()
                    if owned_server == server_id
                ]
                for gate in gates:
                    await gate.lock.acquire()
                    acquired.append(gate)
                    gate.generation += 1

            yield
        finally:
            with self._errors.boundary(f"Unlocking OAuth MCP server {server_id}"):
                for gate in acquired:
                    gate.lock.release()

                self._editing.remove(server_id)

    async def finish(self, login: str, code: str, state: str, iss: str | None) -> str:
        with self._errors.boundary(
            f"Completing MCP OAuth callback for AID user {login!r}"
        ):
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
            if pending.failure is not None:
                raise pending.failure

            return pending.return_url

    def reject(self, login: str, state: str) -> None:
        with self._errors.boundary(
            f"Rejecting MCP OAuth callback for AID user {login!r}"
        ):
            pending = self._states.get(state)
            if pending is None:
                raise McpAppError("MCP OAuth refusal has no matching AID sign-in")

            if pending.login != login:
                raise McpAppError("MCP OAuth refusal has no matching AID sign-in")
            self._forget(pending)

    async def _probe(
        self, pending: PendingOAuth, endpoint: HttpEndpoint | SseEndpoint
    ) -> None:
        try:
            with self._errors.boundary(
                f"Completing OAuth for MCP server {pending.server_id}"
            ):
                await self._connections.connect(pending, endpoint)
            if not pending.url.done():
                pending.url.set_result("")
        except asyncio.CancelledError:
            pending.failure = McpAppError("MCP OAuth sign-in was cancelled")
            raise
        except McpAppError as exc:
            pending.failure = exc
            if not pending.url.done():
                pending.url.set_exception(exc)
        finally:
            pending.completed.set()
            self._forget(pending)

    def _forget(self, pending: PendingOAuth) -> None:
        if not pending.url.done():
            pending.url.set_exception(McpAppError("MCP OAuth sign-in was cancelled"))

        key = (pending.server_id, pending.user_id)
        if self._pending.get(key) is pending:
            self._pending.pop(key)

        if pending.state:
            if self._states.get(pending.state) is pending:
                self._states.pop(pending.state)

        task = pending.task
        if task is None:
            return

        if task is asyncio.current_task():
            return

        if not task.done():
            task.cancel()


class McpOAuthCallback:
    """Authenticated AID browser return for the SDK authorization-code flow."""

    ROUTE = "/api/mcp/oauth/callback"

    def __init__(
        self,
        oauth: McpBrowserOAuth,
        tokens: TokenReader,
        cookie: str,
    ) -> None:
        self._oauth = oauth
        self._tokens = tokens
        self._request_tokens = RequestTokens(cookie)
        self._errors = OAuthErrors()
        self._logger = logging.getLogger(__name__)

    async def serve(self, request: Request) -> Response:
        try:
            with self._errors.boundary("Handling AID MCP OAuth callback"):
                return await self._response(request)
        except McpAuthRequired as exc:
            self._logger.exception("MCP OAuth sign-in required: %s", exc)
            return PlainTextResponse(exc.brief, status_code=401)
        except McpOAuthError as exc:
            self._logger.exception("MCP OAuth callback failed: %s", exc)
            return PlainTextResponse(exc.view_message().content, status_code=500)
        except McpAppError as exc:
            self._logger.exception("MCP OAuth callback rejected: %s", exc)
            return PlainTextResponse(exc.view_message().content, status_code=400)

    async def _response(self, request: Request) -> Response:
        token = self._request_tokens.of_request(request)
        if token is None:
            return PlainTextResponse(
                "Sign in to AID before connecting MCP", status_code=401
            )
        try:
            login = self._tokens.read(token).identifier
        except TokenRejectedError:
            return PlainTextResponse("AID sign-in expired", status_code=401)

        return_url = await self._accept(login, request)

        return RedirectResponse(return_url, status_code=303)

    async def _accept(self, login: str, request: Request) -> str:
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
        return await self._oauth.finish(login, code, state, params.get(OAuthParam.ISS))


class McpOAuthClientDocument:
    """Public HTTP route for the client metadata URL advertised to the SDK."""

    ROUTE = "/api/mcp/oauth/client"

    def __init__(self, oauth: McpBrowserOAuth) -> None:
        self._oauth = oauth
        self._errors = OAuthErrors()
        self._logger = logging.getLogger(__name__)

    async def serve(self) -> Response:
        try:
            with self._errors.boundary("Serving AID MCP OAuth client metadata"):
                document = self._oauth.client_document()
                return JSONResponse(document.model_dump(mode="json", exclude_none=True))
        except McpAppError as exc:
            self._logger.exception("MCP OAuth client metadata failed: %s", exc)
            return PlainTextResponse(exc.view_message().content, status_code=500)
        except McpAuthRequired as exc:
            self._logger.exception("MCP OAuth client metadata failed: %s", exc)
            return PlainTextResponse(exc.brief, status_code=500)
