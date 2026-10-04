"""Вход в сервис исполнения: система входа boba как есть поверх MCP.

Способы входа, провайдеры и маппинги ролей — те же, что у чата: секции
[auth.*] конфига сервиса и сервис входа AuthService. Первый подключённый
способ — proxy: доверенный клиент (чат) называет логин и роли пользователя
заголовками под подписью HMAC и получает токен сессии сервиса. Токен
предъявляется на endpoint'ах MCP как Bearer; проверка — локальная, по
подписи. Готовые токены секции [mcp.tokens] принимаются наравне.

Ошибки:
TokenClaimsError — токен принят, но логина и ролей вызывающего в нём нет.
CallScopeError — область вызова из _meta не годится сегментом пути.
маршруты входа и ворота отвечают по HTTP: 401 — подпись, окно времени или токен не
    приняты; 403 — вход запрещён (адрес клиента, исключение, нет ролей) либо
    endpoint не выдан ролям вошедшего.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_context
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as AsgiScope

from boba.access import ProfileGrant
from boba.auth import AuthService
from boba.auth.config import ProxyAuthConfig
from boba.identity.context import Scope, Subject
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.token import TokenRejectedError
from boba.runtime.http import ProxyRequests
from boba.toolkit.wire import RequestMeta

__all__ = [
    "CallScopeError",
    "CallScopes",
    "EndpointGate",
    "IssuedToken",
    "ProxySignInRoute",
    "SessionTokenVerifier",
    "TokenClaim",
    "TokenClaims",
    "TokenClaimsError",
    "TokenSubjects",
]

logger = logging.getLogger(__name__)


class TokenClaim(StrEnum):
    """Клеймы токена доступа, по которым сервис узнаёт вызывающего."""

    LOGIN = "login"
    ROLES = "roles"
    USER_ID = "user_id"


class TokenClaimsError(Exception):
    """Токен принят, но вызывающего по нему не узнать."""


class TokenClaims(BaseModel):
    """Клеймы вызывающего в токене доступа.

    login и roles обязательны. user_id несёт готовый токен конфига, у
    которого он задан; без него сервис выводит идентификатор из логина.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    roles: frozenset[str]
    user_id: UUID | None = None


class TokenSubjects:
    """Субъект вызова по токену доступа.

    Создаётся сервером endpoint'а с его профилем; им пользуются провайдер
    инструментов (по ролям и профилю — список) и middleware контекста (по
    субъекту — контекст вызова).
    """

    def __init__(self, profile: str) -> None:
        self._profile = profile

    def current(self) -> Subject:
        """Субъект токена текущего запроса; без токена — TokenClaimsError."""
        token = get_access_token()
        if token is None:
            msg = (
                "resolving the caller: the request carries no access token, "
                "expected a bearer token with login and roles"
            )
            raise TokenClaimsError(msg)

        return self.of(token)

    def of(self, token: AccessToken) -> Subject:
        try:
            claims = TokenClaims.model_validate(token.claims)
        except ValidationError as exc:
            msg = (
                f"resolving the caller of client {token.client_id!r}: the token "
                f"claims do not carry login and roles: {exc}"
            )
            raise TokenClaimsError(msg) from exc

        user_id = claims.user_id
        if user_id is None:
            user_id = uuid5(NAMESPACE_URL, f"boba-mcp:{claims.login}")

        return Subject(
            user_id=user_id,
            login=claims.login,
            roles=claims.roles,
            profile=self._profile,
        )


class CallScopeError(Exception):
    """Клиент прислал область, которая не годится сегментом пути."""


class CallScopes:
    """Область вызова по _meta запроса.

    Создаётся сборкой сервера, зовёт её CallContextMiddleware. Область —
    место файлов и описаний вызова в workspace пользователя; клиент с
    собственными разговорами (чат) присылает в ней id разговора, чтобы
    вызовы одного разговора видели файлы друг друга. Клиент без области
    получает область по умолчанию — id самого пользователя, то есть свою
    у каждого пользователя.
    """

    def of(self, subject: Subject) -> Scope:
        """Область текущего запроса; негодный id области — CallScopeError."""
        sent = self._sent()
        if sent is None:
            return Scope.chat(str(subject.user_id))

        try:
            return Scope.chat(sent)
        except ValidationError as exc:
            msg = (
                f"resolving the scope of the call: _meta[{RequestMeta.SCOPE.value!r}] "
                f"expects one path segment, got {sent!r}: {exc}"
            )
            raise CallScopeError(msg) from exc

    @staticmethod
    def _sent() -> str | None:
        request = get_context().request_context
        if request is None:
            return None

        meta = request.meta
        if meta is None:
            return None

        sent = meta.get(RequestMeta.SCOPE)
        if not isinstance(sent, str):
            return None

        if not sent:
            return None

        return sent


class IssuedToken(BaseModel):
    """Ответ входа: токен сессии сервиса и срок его жизни в секундах."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: str = Field(min_length=1)
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0)


class SessionTokenVerifier(TokenVerifier):
    """Проверка токена на endpoint'ах MCP: токен сессии сервиса входа либо
    готовый токен конфига.

    Создаётся сборкой процесса из сервиса входа и токенов [mcp.tokens];
    отдаётся в FastMCP(auth=…) каждого endpoint'а и воротам EndpointGate.
    Токен сессии проверяется подписью и сроком, без обращения к хранилищу.
    Клеймы login и roles читает TokenSubjects.
    """

    CLAIM_LOGIN: ClassVar[str] = "login"
    CLAIM_ROLES: ClassVar[str] = "roles"

    def __init__(
        self, auth: AuthService, tokens: Mapping[str, Mapping[str, Any]]
    ) -> None:
        super().__init__()
        self._auth = auth
        self._tokens = dict(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Вызывающий по токену; None — токен не принят (401)."""
        static = self._tokens.get(token)
        if static is not None:
            return AccessToken(
                token=token,
                client_id=str(static[self.CLAIM_LOGIN]),
                scopes=[],
                claims=dict(static),
            )

        try:
            claims = self._auth.tokens.read(token)
        except TokenRejectedError as exc:
            logger.info("access token is rejected as %s: %s", exc.reason, exc)
            return None

        roles = sorted(claims.sign_in().roles)

        return AccessToken(
            token=token,
            client_id=claims.identifier,
            scopes=[],
            expires_at=claims.exp,
            claims={self.CLAIM_LOGIN: claims.identifier, self.CLAIM_ROLES: roles},
        )


class ProxySignInRoute:
    """Маршрут входа proxy: POST [auth.proxy].path.

    Создаётся сборкой процесса, когда в [auth] есть proxy. Заголовки
    доверенного клиента читает по именам из того же конфига и отдаёт сервису
    входа; ответ — токен сессии сервиса телом JSON, клиент MCP предъявляет
    его как Bearer.
    """

    BEARER: ClassVar[Literal["Bearer"]] = "Bearer"

    def __init__(self, config: ProxyAuthConfig, auth: AuthService) -> None:
        self._config = config
        self._auth = auth

    def route(self) -> Route:
        return Route(self._config.path, self.sign_in, methods=["POST"])

    async def sign_in(self, request: Request) -> Response:
        proxy_request = ProxyRequests.of(request, self._config.header_names())
        try:
            session = await self._auth.by_proxy(proxy_request)
        except AuthenticationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        except AuthorizationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)

        logger.info(
            "proxy sign-in [user=%s] [client=%s] [roles=%s]",
            session.signed.identifier,
            proxy_request.client,
            ",".join(sorted(session.signed.sign_in.roles)),
        )
        issued = IssuedToken(
            access_token=session.token,
            token_type=self.BEARER,
            expires_in=self._auth.cookie().ttl_sec,
        )

        return JSONResponse(issued.model_dump(mode="json"))


class EndpointGate:
    """Ворота endpoint'а MCP: пускает вошедшего, которому endpoint выдан.

    Создаётся сборкой endpoint'ов вокруг приложения одного endpoint'а из
    проверяющего токены и гранта профиля. Запрос без токена или с негодным
    токеном идёт дальше — приложение ответит 401; токен принят, а ролям
    вошедшего endpoint не выдан — 403.
    """

    BEARER_PREFIX: ClassVar[str] = "bearer "

    def __init__(
        self, app: ASGIApp, verifier: TokenVerifier, name: str, grant: ProfileGrant
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._name = name
        self._grant = grant

    async def __call__(self, scope: AsgiScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        token = self._bearer(request)
        if token is None:
            await self._app(scope, receive, send)
            return

        access = await self._verifier.verify_token(token)
        if access is None:
            await self._app(scope, receive, send)
            return

        roles = access.claims.get(SessionTokenVerifier.CLAIM_ROLES, [])
        if self._grant.visible_for(roles):
            await self._app(scope, receive, send)
            return

        message = (
            f"403 Forbidden: mcp endpoint {self._name!r} is not granted to the "
            f"roles {sorted(roles)} of {access.client_id!r}"
        )
        logger.warning("%s", message)
        refusal = JSONResponse({"error": message}, status_code=403)
        await refusal(scope, receive, send)

    def _bearer(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith(self.BEARER_PREFIX):
            return None

        return header[len(self.BEARER_PREFIX) :].strip()
