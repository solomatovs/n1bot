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
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from boba.access import ProfileGrant
from boba.cancellation import StopReason
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.mcp_server.auth import SessionAuthProvider, TokenClaim
from boba.mcp_server.server import (
    EndpointCatalog,
    McpEndpoints,
    RunLimits,
)
from boba.runtime import providers
from boba.runtime.config import AppName, ProcessConfig
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
    запусков, endpoint'ы и готовые токены доступа. Endpoint
    [mcp.endpoints.<имя>] — набор инструментов (tools) и роли, которым он
    доступен (roles); отвечает на `{path}/{имя}`. Вход — секции [auth.*]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    path: str = Field(min_length=1)
    limits: RunLimits
    endpoints: dict[str, ProfileGrant] = Field(min_length=1)
    tokens: dict[str, McpToken]
    """Токены доступа по именам записей конфига."""


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
        self._container.provide(providers.app_name, AppName.MCP)
        self._container.provide(providers.seal_keys, SealKeys())
        self._container.provide(providers.call_contexts, CallContexts())
        # гранты и каталог профилей входа у сервиса — его endpoint'ы, а не
        # профили чата; роль вошедшего решает только доступ к endpoint'у
        endpoints = EndpointCatalog(config.mcp.endpoints)
        self._container.provide(providers.tool_grants, endpoints.grants())
        self._container.provide(providers.profile_catalog, endpoints)
        self._container.eager(providers.message_bus)
        self._container.eager(providers.connection_types)
        self._container.eager(providers.call_ambient)
        self._container.eager(providers.runs)
        self._container.eager(providers.call_journals)
        self._container.eager(providers.credential_source)
        self._container.eager(providers.session_tokens)
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
            auth = SessionAuthProvider(
                self._container.resolved(providers.sign_ins),
                self._container.resolved(providers.session_tokens),
                self._config.proxy(),
                self._tokens(section),
            )
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

    @staticmethod
    def _tokens(section: McpSection) -> dict[str, dict[str, object]]:
        tokens: dict[str, dict[str, object]] = {}
        for declared in section.tokens.values():
            tokens[declared.token.get_secret_value()] = declared.claims()

        return tokens


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
