"""Процесс сервиса исполнения: сборка объектов и запуск MCP-сервера.

`boba-mcp --config <toml>` читает конфиг, поднимает контейнер общих сервисов
(журналы вызовов, реестр запусков, способ запуска, реестр инструментов,
способы входа), строит endpoint'ы MCP над реестром — по одному на профиль
конфига — и слушает адрес секции [mcp].

Ошибки:
SystemExit — конфиг не прочитан либо сервис не поднялся; текст называет причину.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.config
from pathlib import Path
from uuid import UUID

import uvicorn
from fastmcp.server.auth import AccessToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from boba.access import ProfileGrant
from boba.auth.credentials import NoRefresh
from boba.auth.proxy import ProxyAssertions
from boba.cancellation import StopReason
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.mcp_server.auth import (
    AuthServer,
    McpClient,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
    TokenClaim,
)
from boba.mcp_server.server import (
    EndpointCatalog,
    McpEndpoints,
    RunLimits,
)
from boba.runtime import providers
from boba.runtime.config import ProcessConfig
from boba.runtime.di import Container
from boba.runtime.plugins import EntryPointPlugins
from boba.runtime.storage import LocalStorageConfig
from boba.toolkit.types import StringList

__all__ = ["McpAppConfig", "McpEntry", "McpHost", "McpSection", "McpToken"]

logger = logging.getLogger(__name__)


class McpToken(BaseModel):
    """Готовый токен доступа из конфига и вызывающий, которого он означает.

    Вход клиента без утверждений о пользователе (скрипт, сторонний клиент
    MCP): он предъявляет токен, сервис узнаёт по нему логин и роли.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretStr
    login: str = Field(min_length=1)
    roles: StringList
    user_id: UUID | None = None

    def claims(self) -> dict[str, object]:
        claims: dict[str, object] = {
            "client_id": self.login,
            "scopes": [],
            TokenClaim.LOGIN.value: self.login,
            TokenClaim.ROLES.value: list(self.roles),
        }
        if self.user_id is not None:
            claims[TokenClaim.USER_ID.value] = str(self.user_id)

        return claims


class McpSection(BaseModel):
    """Секция [mcp]: адрес сервиса, общий путь endpoint'ов MCP, пределы
    запусков, endpoint'ы, готовые токены доступа и доверенные клиенты. Endpoint
    [mcp.endpoints.<имя>] — набор инструментов (tools) и роли, которым он
    доступен (roles); отвечает на `{path}/{имя}`. Вход — секции [auth.*]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    path: str = Field(min_length=1)
    limits: RunLimits
    public_url: str = Field(min_length=1)
    """Адрес сервиса, каким его видят клиенты (за nginx — с префиксом): от
    него строятся метаданные OAuth, адреса входа и издатель токена. Вне
    localhost — только https."""
    endpoints: dict[str, ProfileGrant] = Field(min_length=1)
    tokens: dict[str, McpToken]
    """Токены доступа по именам записей конфига."""
    clients: dict[str, McpClient]
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class McpAppConfig(ProcessConfig):
    """Секции процесса сервиса: секции процесса, [mcp] и хранилище workspace."""

    mcp: McpSection
    storage: LocalStorageConfig
    """Хранилище workspace: туда пишут маршруты файлов и оттуда читают
    инструменты."""


class McpHost:
    """Сборка процесса сервиса: контейнер общих сервисов и MCP-сервер над
    реестром инструментов. Создаётся входом процесса из конфига."""

    def __init__(self, config: McpAppConfig) -> None:
        self._config = config
        self._container = Container(level="app")
        self._container.provide(providers.get_process_config, config)
        self._container.provide(providers.plugin_table, EntryPointPlugins.discover)
        self._container.provide(providers.seal_keys, SealKeys())
        self._container.provide(providers.call_contexts, CallContexts())
        # сигнал «обнови билет входа» слушает страница чата: у сервиса её нет
        self._container.provide(providers.refresh_signal, NoRefresh())
        # гранты и каталог профилей входа у сервиса — его endpoint'ы, а не
        # профили чата; роль вошедшего решает только доступ к endpoint'у
        self._endpoints = EndpointCatalog(config.mcp.endpoints)
        self._container.provide(providers.tool_grants, self._endpoints.grants())
        self._container.provide(providers.profile_catalog, self._endpoints)
        self._container.eager(providers.connection_types)
        self._container.eager(providers.call_ambient)
        self._container.eager(providers.runs)
        self._container.eager(providers.call_journals)
        self._container.eager(providers.credential_source)
        self._container.eager(providers.user_directory)
        self._container.eager(providers.sign_ins)
        self._container.eager(providers.tool_launchers)
        self._container.eager(providers.tool_registry)

    async def serve(self) -> None:
        """Поднимает сервисы, слушает адрес секции [mcp] до остановки процесса
        и гасит запуски и зиготы на выходе."""
        section = self._config.mcp
        Container.set_root(self._container)
        await self._container.start()
        try:
            auth = self._auth(section)
            endpoints = McpEndpoints(
                self._container.resolved(providers.tool_registry),
                self._container.resolved(providers.runs),
                self._container.resolved(providers.call_journals),
                auth,
                section.limits,
                section.path,
                self._config.storage,
            )
            listener = uvicorn.Server(
                uvicorn.Config(
                    endpoints.app(),
                    host=section.host,
                    port=section.port,
                    log_config=None,
                )
            )
            logger.info(
                "boba-mcp listens on %s:%d, mcp endpoints: %s",
                section.host,
                section.port,
                ", ".join(endpoints.paths()),
            )
            await listener.serve()
        finally:
            self._container.resolved(providers.runs).stop_all(StopReason.SHUTDOWN)
            self._container.resolved(providers.tool_launchers).stop()
            Container.set_root(None)
            await self._container.aclose()

    def _auth(self, section: McpSection) -> ServiceAuth:
        """Вход сервиса: сервер авторизации над способами входа [auth] и
        проверяющие токена endpoint'ов."""
        session = self._config.session
        public_url = section.public_url.rstrip("/")
        generation = session.session_generation()
        logger.info("session generation of this process: %s", generation)
        tokens = ServiceTokens(
            public_url,
            session.auth_secret,
            session.session_ttl_sec,
            session.session_max_sec,
            generation,
        )
        assertions = None
        if proxy := self._config.proxy():
            assertions = ProxyAssertions(
                proxy.secret.get_secret_value(), proxy.max_skew_sec
            )

        sealed = SealedValues(session.auth_secret)
        resources: list[str] = []
        for name in section.endpoints:
            resources.append(f"{public_url}{section.path}/{name}")

        server = AuthServer(
            public_url,
            self._container.resolved(providers.sign_ins),
            tokens,
            assertions,
            self._clients(section, sealed),
            sealed,
            resources,
        )

        return ServiceAuth(public_url, server, tokens, self._static(section))

    def _clients(self, section: McpSection, sealed: SealedValues) -> RegisteredClients:
        """Клиенты OAuth: доверенные из [mcp.clients] и регистрирующиеся сами."""
        declared: dict[str, OAuthClientInformationFull] = {}
        for client_id, client in section.clients.items():
            declared[client_id] = client.registered(client_id)

        return RegisteredClients(sealed, declared)

    def _static(self, section: McpSection) -> dict[str, AccessToken]:
        """Готовые токены конфига как вошедшие: области — endpoint'ы,
        выданные ролям токена."""
        static: dict[str, AccessToken] = {}
        for declared in section.tokens.values():
            token = declared.token.get_secret_value()
            granted = self._endpoints.granted_by_roles(frozenset(declared.roles))
            static[token] = AccessToken(
                token=token,
                client_id=declared.login,
                scopes=sorted(granted),
                claims=declared.claims(),
            )

        return static


class McpEntry:
    """Вход процесса: `boba-mcp --config <toml>`."""

    def config_argument(self) -> Path:
        """Путь конфига — обязательный аргумент запуска; дефолта и env нет."""
        parser = argparse.ArgumentParser(
            prog="boba-mcp", description="Tool execution service of boba"
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help="path to the service config.toml",
        )

        return parser.parse_args().config

    async def run(self) -> None:
        config = McpAppConfig.load(self.config_argument())
        logging.config.dictConfig(config.logger)

        await McpHost(config).serve()


async def main() -> None:
    try:
        await McpEntry().run()
    except Exception as exc:
        raise SystemExit(f"boba-mcp failed: {type(exc).__name__}: {exc}") from exc


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
