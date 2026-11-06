"""Сервер протокола OAuth над общими сценариями входа и сессии."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar
from urllib.parse import urlencode

from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.auth import TokenHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.routing import Route

from boba.auth import SessionService, SignIns
from boba.auth.config import ProxyAuthConfig
from boba.identity.authentication import AccessGrant
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    IdentityConflictError,
)
from boba.identity.signin import ProxyRequest, SignInResult
from boba.oauth.assertions import AssertionRejectedError
from boba.oauth.server.assertions import ProxyAssertionVerifier
from boba.oauth.server.clients import RegisteredClients
from boba.oauth.server.codes import GrantedLogin, PendingLogin
from boba.oauth.server.proxy import ProxyLogin
from boba.oauth.server.sealed import SealedValues
from boba.oauth.server.tokens import ServiceTokens, TokenHolder

logger = logging.getLogger(__name__)


class AuthServer(OAuthProvider):
    """Сервер авторизации службы: OAuthProvider fastmcp над ядром входа.

    Создаётся сборкой процесса службы из способов входа SignIns, выпуска
    токенов ServiceTokens, проверки утверждений proxy, клиентов
    RegisteredClients и адресов её ресурсов; его маршруты (/authorize,
    /token, /register) служба ставит под своим префиксом, метаданные — в
    корень хоста (ServiceAuth.app_routes), страницы входа — LoginPages. Вход человека:
    authorize() отдаёт адрес страницы входа с начатым входом, та зовёт
    signed_in() и получает адрес возврата с кодом. Вход proxy:
    exchange_identity_assertion() либо exchange_proxy_headers(). Вход человека
    продлевается токеном обновления до потолка [session].session_max_sec.
    Вход proxy токена обновления не получает — клиент повторяет обмен сам.

    Состояния между запросами нет: начатый вход и код авторизации —
    запечатанные значения (SealedValues), рестарт посреди входа его не рвёт.
    Код авторизации поэтому не гасится первым обменом, а живёт свои
    CODE_SEC; увести вход им нельзя — обмен требует секрет PKCE клиента,
    начавшего вход.
    """

    LOGIN_PATH: ClassVar[str] = "/login"
    TXN: ClassVar[str] = "txn"
    PENDING_SEC: ClassVar[int] = 300
    CODE_SEC: ClassVar[int] = 60
    TOKEN_PATH: ClassVar[str] = "/token"  # noqa: S105 — путь маршрута
    METADATA_PATH: ClassVar[str] = "/.well-known/oauth-authorization-server"

    def __init__(  # noqa: PLR0913 — сервер собирается всеми своими входами
        self,
        public_url: str,
        sign_ins: SignIns,
        tokens: ServiceTokens,
        proxies: Sequence[ProxyAuthConfig],
        clients: RegisteredClients,
        sealed: SealedValues,
        resources: Sequence[str],
        sessions: SessionService,
    ) -> None:
        super().__init__(
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=False),
        )
        self._public_url = public_url.rstrip("/")
        self._sign_ins = sign_ins
        self._tokens = tokens
        self._proxies = tuple(proxies)
        self._assertions = ProxyAssertionVerifier(self._proxies)
        self._resources = frozenset(resources)
        self._clients = clients
        self._sealed = sealed
        self._sessions = sessions

    def sign_ins(self) -> SignIns:
        return self._sign_ins

    def login_url(self, txn: str) -> str:
        return f"{self._public_url}{self.LOGIN_PATH}?{urlencode({self.TXN: txn})}"

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.find(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients.register(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Адрес страницы входа с начатым входом."""
        if client.client_id is None:
            raise AuthorizeError(
                error="invalid_request",
                error_description="authorize: the client has no client_id",
            )

        self._resource(params.resource, AuthorizeError)
        txn = self._sealed.seal(PendingLogin(client_id=client.client_id, params=params))

        return self.login_url(txn)

    def pending(self, txn: str) -> PendingLogin | None:
        """Начатый вход по его идентификатору; None — не начат или истёк."""
        return self._sealed.opened(txn, PendingLogin, self.PENDING_SEC)

    def signed_in(self, held: PendingLogin, signed: SignInResult) -> str:
        """Завершает начатый вход: адрес возврата клиента с кодом авторизации."""
        params = held.params
        code = self._sealed.seal(
            GrantedLogin(
                client_id=held.client_id,
                params=params,
                signed=signed,
                expires_at=time.time() + self.CODE_SEC,
            )
        )
        logger.info(
            "sign-in [user=%s] [provider=%s] [roles=%s]",
            signed.identifier,
            signed.provider,
            ",".join(sorted(signed.access.roles)),
        )

        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        granted = self._sealed.opened(authorization_code, GrantedLogin, self.CODE_SEC)
        if granted is None:
            return None

        if granted.client_id != client.client_id:
            return None

        params = granted.params

        return AuthorizationCode(
            code=authorization_code,
            scopes=sorted(granted.signed.access.profiles),
            expires_at=granted.expires_at,
            client_id=granted.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=granted.signed.identifier,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        granted = self._sealed.opened(
            authorization_code.code, GrantedLogin, self.CODE_SEC
        )
        if granted is None:
            raise TokenError(
                "invalid_grant", "the authorization code is unknown or has expired"
            )

        resource = self._resource(granted.params.resource, TokenError)

        holder = await self._holder(granted.signed, resource)

        return self._tokens.session(holder, granted.client_id)

    async def _holder(self, signed: SignInResult, resource: str) -> TokenHolder:
        try:
            session = await self._sessions.accept(signed)
        except IdentityConflictError as exc:
            raise TokenError("invalid_grant", str(exc)) from exc
        return TokenHolder(
            principal=session.principal,
            credential=session.credential,
            started=int(time.time()),
            resource=resource,
        )

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """Вход proxy: утверждение доверенного клиента о пользователе меняется
        на токен сервиса; роли пользователя решают провайдеры [auth.proxy]."""
        if not self._assertions.configured:
            raise TokenError(
                "unsupported_grant_type",
                "identity assertion: [auth] has no proxy provider configured",
            )

        client_id = str(client.client_id)
        try:
            claims = self._assertions.read(
                params.assertion, client_id, self._public_url
            )
        except AssertionRejectedError as exc:
            logger.warning("%s", exc)
            raise TokenError("invalid_grant", str(exc)) from exc

        resource = self._resource(claims.resource, TokenError)
        self._require_proxy_client(client_id)

        request = ProxyRequest(
            login=claims.identity.login,
            roles=claims.roles,
            client=client_id,
            identity=claims.identity,
        )
        try:
            signed = await self._sign_ins.admit_proxy(request)
        except (AuthenticationError, AuthorizationError) as exc:
            logger.warning("%s", exc)
            raise TokenError("invalid_grant", str(exc)) from exc
        except ExternalServiceError as exc:
            logger.error("%s", exc)
            message = (
                f"identity assertion of {claims.sub!r} from {client_id}: the "
                f"service could not decide the roles of the user: {exc}"
            )
            raise TokenError("invalid_grant", message) from exc

        return await self._proxy_token(signed, resource, client_id)

    async def exchange_proxy_headers(
        self,
        client: OAuthClientInformationFull,
        request: ProxyRequest,
        resource: str,
    ) -> OAuthToken:
        """Общий proxy-вход по заголовкам; клиент уже проверен HTTP-адаптером."""
        client_id = str(client.client_id)
        self._require_proxy_client(client_id)
        resource = self._resource(resource, TokenError)
        signed = await self._sign_ins.by_proxy(request)
        return await self._proxy_token(signed, resource, client_id)

    def _require_proxy_client(self, client_id: str) -> None:
        if not self._clients.permits(client_id):
            raise TokenError(
                "invalid_grant",
                f"client {client_id!r} is not a trusted assertion client",
            )

    async def _proxy_token(
        self, signed: SignInResult, resource: str, client_id: str
    ) -> OAuthToken:
        logger.info(
            "proxy sign-in [user=%s] [client=%s] [roles=%s]",
            signed.identifier,
            client_id,
            ",".join(sorted(signed.access.roles)),
        )

        holder = await self._holder(signed, resource)

        return self._tokens.access(holder, client_id)

    def _resource(
        self, resource: str | None, refusal: type[AuthorizeError | TokenError]
    ) -> str:
        """Адрес ресурса, для которого просят токен, каким его знает
        служба; без ресурса и с чужим ресурсом — отказ refusal."""
        known = ", ".join(sorted(self._resources))
        if resource is None:
            raise refusal(
                "invalid_target",
                "the request names no resource: parameter resource expects the "
                f"address of a resource of this service; known: {known}",
            )

        named = resource.rstrip("/")
        if named in self._resources:
            return named

        raise refusal(
            "invalid_target",
            f"resource {resource!r} is not a resource of this service; known: {known}",
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        held = self._tokens.refreshed(refresh_token)
        if held is None:
            return None

        if held.client_id != client.client_id:
            return None

        return held

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Новая пара токенов по токену обновления, без формы входа."""
        if refresh_token.client_id != client.client_id:
            raise TokenError(
                "invalid_grant", "the refresh token belongs to another client"
            )

        requested = frozenset(scopes)
        if not requested.issubset(refresh_token.scopes):
            raise TokenError(
                "invalid_scope", "requested scopes exceed the refresh token grant"
            )

        holder = self._tokens.refresh_holder(refresh_token)
        access = await self._refresh_access(holder)
        access = access.model_copy(update={"profiles": requested & access.profiles})
        principal = holder.principal.model_copy(update={"access": access})
        holder = replace(holder, principal=principal)

        return self._tokens.session(holder, refresh_token.client_id)

    async def _refresh_access(self, holder: TokenHolder) -> AccessGrant:
        self._resource(holder.resource, TokenError)
        try:
            principal = await self._sessions.current(holder.principal)
        except AuthenticationError as exc:
            raise TokenError("invalid_grant", str(exc)) from exc
        return principal.access

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._tokens.read(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Маршруты OAuthProvider с обменом утверждения: базовый класс собирает
        /token и метаданные без него."""
        routes: list[Route] = []
        for route in super().get_routes(mcp_path):
            if route.path == self.TOKEN_PATH:
                routes.append(self._token_route())
                continue

            if route.path == self.METADATA_PATH:
                routes.append(self._metadata_route(route))
                continue

            routes.append(route)

        for config in self._proxies:
            if config.path == self.LOGIN_PATH or any(
                route.path == config.path for route in routes
            ):
                msg = f"proxy path {config.path!r} conflicts with auth routes"
                raise ValueError(msg)
            routes.append(
                Route(
                    config.path,
                    endpoint=ProxyLogin(self, config).handle,
                    methods=["POST"],
                )
            )

        return routes

    def _token_route(self) -> Route:
        handler = TokenHandler(
            provider=self,
            client_authenticator=ClientAuthenticator(self),
            identity_assertion_enabled=True,
        )

        return Route(
            self.TOKEN_PATH,
            endpoint=cors_middleware(handler.handle, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        )

    def _metadata_route(self, route: Route) -> Route:
        metadata = build_metadata(
            AnyHttpUrl(self._public_url),
            None,
            ClientRegistrationOptions(enabled=True),
            RevocationOptions(enabled=False),
            supports_identity_assertion=True,
        )
        handler = MetadataHandler(metadata)

        return Route(
            route.path,
            endpoint=cors_middleware(handler.handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
