"""Подписанные proxy-заголовки → токен API службы MCP или BOBA."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mcp.server.auth.middleware.client_auth import (
    AuthenticationError as ClientAuthenticationError,
)
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import TokenError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from boba.auth.config import ProxyAuthConfig
from boba.auth.transport.http import ProxyRequests
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
)

if TYPE_CHECKING:
    from boba.oauth.server.provider import AuthServer


class ProxyLogin:
    """POST на ProxyAuthConfig.path с общими заголовками входа.

    Форма называет client_id, client_secret и resource. Клиент проходит ту же
    проверку, что на /token; подпись заголовков проверяет общий boba-auth.
    Выдаётся только access_token, как при обмене JWT-утверждения.
    """

    HEADERS: ClassVar[dict[str, str]] = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }

    def __init__(self, server: AuthServer, config: ProxyAuthConfig) -> None:
        self._server = server
        self._names = config.header_names()
        self._clients = ClientAuthenticator(server)
        self._requests = ProxyRequests()

    async def handle(  # noqa: PLR0911 — отдельный HTTP-статус для каждого отказа
        self, request: Request
    ) -> Response:
        try:
            client = await self._clients.authenticate_request(request)
        except ClientAuthenticationError as exc:
            return self._error("invalid_client", exc.message, 401)

        fields = await request.form()
        resource = fields.get("resource")
        if not isinstance(resource, str) or not resource:
            return self._error("invalid_target", "resource is required", 400)

        proxy = self._requests.of(request, self._names)
        try:
            token = await self._server.exchange_proxy_headers(client, proxy, resource)
        except AuthenticationError as exc:
            return self._error("invalid_grant", str(exc), 401)
        except AuthorizationError as exc:
            return self._error("access_denied", str(exc), 403)
        except ExternalServiceError as exc:
            return self._error("temporarily_unavailable", str(exc), 503)
        except TokenError as exc:
            return self._error(exc.error, exc.error_description or "", 400)

        return JSONResponse(
            token.model_dump(mode="json", exclude_none=True), headers=self.HEADERS
        )

    def _error(self, error: str, description: str, status: int) -> Response:
        return JSONResponse(
            {"error": error, "error_description": description},
            status_code=status,
            headers=self.HEADERS,
        )
