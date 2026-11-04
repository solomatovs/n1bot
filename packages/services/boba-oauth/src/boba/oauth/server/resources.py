"""Проверка ресурсов и подключение маршрутов сервера OAuth к приложению."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from pydantic import AnyHttpUrl
from starlette.routing import BaseRoute, Mount

from boba.identity.signin import ProfileCatalog
from boba.oauth.claims import TokenClaims
from boba.oauth.server.pages import LoginPages
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.tokens import ServiceTokens

logger = logging.getLogger(__name__)


class ResourceTokens(TokenVerifier):
    """Проверка токена на одном ресурсе службы: токен службы, выпущенный для
    этого адреса; области scopes обязательны.

    Создаётся входом службы на каждый ресурс (endpoint MCP у boba-mcp, адрес
    ACP у службы агента) и отдаётся в RemoteAuthProvider: без токена и с
    токеном другого ресурса ответ — 401 со ссылкой на метаданные ресурса, без
    обязательной области — 403 insufficient_scope. Подпись, срок, поколение,
    resource и scopes проверяются по JWT; запрос к users/user_roles не делается.
    """

    def __init__(
        self,
        tokens: ServiceTokens,
        resource: str,
        scopes: Sequence[str],
        profiles: ProfileCatalog | None = None,
    ) -> None:
        super().__init__(required_scopes=list(scopes))
        self._tokens = tokens
        self._resource = resource
        self._profiles = profiles

    async def verify_token(self, token: str) -> AccessToken | None:
        """Локально проверяет JWT; None — токен не принят (401)."""
        read = self._tokens.read(token)
        if read is None:
            return None

        if read.resource != self._resource:
            logger.info(
                "access token of client %r is issued for %r, this resource "
                "accepts tokens for %r",
                read.client_id,
                read.resource,
                self._resource,
            )
            return None

        if self._profiles is not None:
            claims = TokenClaims.model_validate(read.claims)
            permitted = self._profiles.granted_by_roles(claims.roles)
            scopes = sorted(set(read.scopes) & permitted)
            read = read.model_copy(update={"scopes": scopes})

        return read


class ServiceAuth:
    """Вход службы целиком: сервер авторизации, страницы входа и проверяющие
    токена её ресурсов.

    Создаётся сборкой процесса службы (boba-mcp, служба агента) с её именем
    на странице входа и ресурсами по именам; сборка приложения берёт
    провайдер авторизации на каждый ресурс и маршруты приложения под
    префиксом службы (app_routes).
    """

    def __init__(  # noqa: PLR0913 — сборка общего входа службы
        self,
        public_url: str,
        server: AuthServer,
        tokens: ServiceTokens,
        service: str,
        resources: Mapping[str, str],
        *,
        profiles: ProfileCatalog | None = None,
    ) -> None:
        self._public_url = public_url
        self._server = server
        self._pages = LoginPages(server, public_url, service)
        self._tokens = tokens
        self._resources = dict(resources)
        self._profiles = profiles

    def published(self) -> str:
        """Префикс пути, под которым служба отвечает: путь публичного адреса
        (равен url_prefix секции службы, ServiceAddress); в корне — пустой."""
        return urlsplit(self._public_url).path.rstrip("/")

    def resource(self, name: str, scopes: Sequence[str]) -> RemoteAuthProvider:
        """Провайдер авторизации ресурса name: принимает только токен этого
        ресурса с областями scopes и объявляет сервер авторизации в
        метаданных ресурса (RFC 9728)."""
        return RemoteAuthProvider(
            token_verifier=ResourceTokens(
                self._tokens, self._resources[name], scopes, self._profiles
            ),
            authorization_servers=[AnyHttpUrl(self._public_url)],
            base_url=self._public_url,
        )

    def app_routes(
        self, discovery: Sequence[BaseRoute], served: Sequence[BaseRoute]
    ) -> list[BaseRoute]:
        """Маршруты приложения службы под её префиксом.

        Метаданные (RFC 8414, RFC 9728) клиент ищет от корня хоста по пути с
        префиксом внутри — они стоят в корне: метаданные сервера авторизации и
        discovery ресурсов службы. Всё остальное — сервер авторизации, страницы
        входа и served службы — отвечает под префиксом публичного адреса:
        прокси передаёт путь как есть.
        """
        mounted: list[BaseRoute] = list(self._server.get_routes())
        mounted.extend(self._pages.routes())
        mounted.extend(served)

        routes: list[BaseRoute] = list(self._server.get_well_known_routes())
        routes.extend(discovery)
        routes.append(Mount(self.published(), routes=mounted))

        return routes
