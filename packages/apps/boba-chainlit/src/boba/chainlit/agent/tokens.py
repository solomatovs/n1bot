"""Серверы MCP сессии агента с готовыми заголовками: токен `boba-mcp` chainlit
получает сам обменом RFC 7523 у его сервера авторизации, сервер инструментов
чата получает JWT пользователя.

Ошибки:
McpTokenError — сервер авторизации не нашёлся, отверг утверждение или ответил
    не токеном.
SessionServersError — сервер профиля не того транспорта или без адреса.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus

import httpx
from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
)
from mcp.shared.auth import JWT_BEARER_GRANT_TYPE, OAuthMetadata, OAuthToken
from mcp.shared.auth_utils import resource_url_from_server_url
from pydantic import SecretStr, ValidationError

from boba.auth.proxy import ProxyAssertions
from boba.chainlit.agent.config import ChatMcpConfig
from boba.chainlit.agent.settings import AuthHeader
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    McpServerConfig,
    McpServersConfig,
    NoAuth,
    ProxyAuth,
    StdioCommand,
)

__all__ = [
    "McpAccessToken",
    "McpAccessTokens",
    "McpTokenError",
    "SessionServer",
    "SessionServers",
    "SessionServersError",
]

logger = logging.getLogger(__name__)

SessionServer = HttpMcpServer | McpServerStdio


class McpTokenError(Exception):
    """Обмен утверждения на токен не состоялся."""


class SessionServersError(Exception):
    """Сервер профиля нельзя отдать агенту."""


class TokenForm(StrEnum):
    """Поля формы запроса токена (RFC 7523, RFC 8707)."""

    GRANT_TYPE = "grant_type"
    ASSERTION = "assertion"
    CLIENT_ID = "client_id"
    CLIENT_SECRET = "client_secret"  # noqa: S105 — имя поля формы
    RESOURCE = "resource"


@dataclass(frozen=True)
class McpAccessToken:
    """Токен доступа к endpoint'у boba-mcp и момент его истечения."""

    value: SecretStr
    expires_at: float | None

    def header(self) -> HttpHeader:
        return HttpHeader(
            name=AuthHeader.NAME.value,
            value=AuthHeader.render(self.value.get_secret_value()),
        )


class McpAccessTokens:
    """Клиент OAuth у сервера авторизации boba-mcp: метаданные RFC 8414 по
    issuer, утверждение о пользователе под общим секретом, обмен jwt-bearer.

    Создаётся провайдером процесса; зовут SessionServers перед `session/new`.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def exchange(
        self, auth: ProxyAuth, resource: str, login: str, roles: frozenset[str]
    ) -> McpAccessToken:
        """Ошибки:
        McpTokenError — метаданные не нашлись или обмен отвергнут.
        """
        issuer = auth.issuer.url()
        canonical = resource_url_from_server_url(resource)
        token_endpoint = await self._token_endpoint(issuer, canonical)
        assertions = ProxyAssertions(auth.secret.get_secret_value(), 0)
        assertion = assertions.issue(
            auth.client_id, login, ",".join(sorted(roles)), issuer, canonical
        )
        form = {
            TokenForm.GRANT_TYPE.value: JWT_BEARER_GRANT_TYPE,
            TokenForm.ASSERTION.value: assertion,
            TokenForm.CLIENT_ID.value: auth.client_id,
            TokenForm.CLIENT_SECRET.value: auth.client_secret.get_secret_value(),
            TokenForm.RESOURCE.value: canonical,
        }
        try:
            response = await self._http.post(token_endpoint, data=form)
        except httpx.HTTPError as exc:
            msg = f"POST {token_endpoint} for the token of {canonical}: {exc}"
            raise McpTokenError(msg) from exc

        if response.status_code != HTTPStatus.OK:
            msg = (
                f"POST {token_endpoint} for the token of {canonical} as {login!r}: "
                f"expected 200, got {response.status_code}: {response.text[:300]}"
            )
            raise McpTokenError(msg)

        try:
            token = OAuthToken.model_validate_json(response.content)
        except ValidationError as exc:
            msg = (
                f"POST {token_endpoint}: expected an OAuth token response, "
                f"got {response.text[:300]}: {exc}"
            )
            raise McpTokenError(msg) from exc

        expires_at = None
        if token.expires_in is not None:
            expires_at = time.time() + token.expires_in

        logger.info(
            "mcp token issued: resource=%s login=%s expires_in=%s",
            canonical,
            login,
            token.expires_in,
        )

        return McpAccessToken(
            value=SecretStr(token.access_token), expires_at=expires_at
        )

    async def _token_endpoint(self, issuer: str, resource: str) -> str:
        """Адрес token endpoint из метаданных сервера авторизации issuer.

        Ошибки:
        McpTokenError — ни один адрес метаданных не ответил документом RFC 8414.
        """
        tried: list[str] = []
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            issuer, resource
        ):
            tried.append(url)
            try:
                response = await self._http.get(url)
            except httpx.HTTPError as exc:
                msg = f"GET {url} for the authorization server metadata: {exc}"
                raise McpTokenError(msg) from exc

            if response.status_code != HTTPStatus.OK:
                continue

            try:
                metadata = OAuthMetadata.model_validate_json(response.content)
            except ValidationError as exc:
                msg = (
                    f"GET {url}: expected RFC 8414 metadata, "
                    f"got {response.text[:300]}: {exc}"
                )
                raise McpTokenError(msg) from exc

            return str(metadata.token_endpoint)

        msg = (
            f"authorization server of {issuer} published no metadata at any of "
            f"{tried}; the token for {resource} cannot be requested"
        )
        raise McpTokenError(msg)


class SessionServers:
    """Список `mcpServers` для `session/new` и `session/load`: серверы профиля
    из [mcp.servers] с заголовками и сервер инструментов чата с JWT входа.

    Создаётся провайдером процесса; зовёт объект треда перед открытием сессии.
    """

    def __init__(
        self,
        config: McpServersConfig,
        tokens: McpAccessTokens,
        chat: ChatMcpConfig,
        public_url: str,
    ) -> None:
        self._config = config
        self._tokens = tokens
        self._chat = chat
        self._public_url = public_url

    def hooked(self, names: Sequence[str]) -> list[str]:
        """Имена серверов, чьи вызовы идут через хук запечатывания: серверы
        boba-mcp со входом proxy.

        Ошибки:
        SessionServersError — имя не объявлено в [mcp.servers].
        """
        found: list[str] = []
        for name in names:
            endpoint = self._declared(name).endpoint
            if not isinstance(endpoint, HttpEndpoint):
                continue

            if isinstance(endpoint.auth, ProxyAuth):
                found.append(name)

        return found

    async def of(
        self, names: Sequence[str], login: str, roles: frozenset[str], user_jwt: str
    ) -> list[SessionServer]:
        """Ошибки:
        McpTokenError — токен boba-mcp не получен.
        SessionServersError — сервер профиля не годится агенту.
        """
        servers: list[SessionServer] = []
        for name in names:
            servers.append(await self._of(name, login, roles))

        if self._chat.enable:
            servers.append(self._chat_server(user_jwt))

        return servers

    def _declared(self, name: str) -> McpServerConfig:
        """Ошибки:
        SessionServersError — имя не объявлено в [mcp.servers].
        """
        known = self._config.servers.get(name)
        if known is None:
            declared = sorted(self._config.servers)
            msg = f"mcp server {name!r} is not declared in [mcp.servers]: {declared}"
            raise SessionServersError(msg)

        return known

    async def _of(self, name: str, login: str, roles: frozenset[str]) -> SessionServer:
        endpoint = self._declared(name).endpoint
        if isinstance(endpoint, StdioCommand):
            env: list[EnvVariable] = []
            for key, value in endpoint.env.items():
                env.append(EnvVariable(name=key, value=value))

            return McpServerStdio(
                name=name, command=endpoint.command, args=list(endpoint.args), env=env
            )

        if not isinstance(endpoint, HttpEndpoint):
            msg = (
                f"mcp server {name!r} uses transport {endpoint.transport!r}; the "
                "agent takes streamable http and stdio servers only"
            )
            raise SessionServersError(msg)

        url = endpoint.url()
        headers = await self._headers(name, endpoint, url, login, roles)

        return HttpMcpServer(type="http", name=name, url=url, headers=headers)

    async def _headers(
        self,
        name: str,
        endpoint: HttpEndpoint,
        url: str,
        login: str,
        roles: frozenset[str],
    ) -> list[HttpHeader]:
        auth = endpoint.auth
        if isinstance(auth, NoAuth):
            return []

        if isinstance(auth, BearerAuth):
            return [
                HttpHeader(
                    name=AuthHeader.NAME.value,
                    value=AuthHeader.render(auth.token.get_secret_value()),
                )
            ]

        if isinstance(auth, ProxyAuth):
            token = await self._tokens.exchange(auth, url, login, roles)
            return [token.header()]

        msg = f"mcp server {name!r}: auth {auth!r} has no header form for the agent"
        raise SessionServersError(msg)

    def _chat_server(self, user_jwt: str) -> HttpMcpServer:
        return HttpMcpServer(
            type="http",
            name=self._chat.name,
            url=self._public_url.rstrip("/") + self._chat.path,
            headers=[
                HttpHeader(
                    name=AuthHeader.NAME.value, value=AuthHeader.render(user_jwt)
                )
            ],
        )
