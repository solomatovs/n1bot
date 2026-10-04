"""Процесс сервиса исполнения: сборка объектов и запуск MCP-сервера.

`boba-dag --config <toml>` читает конфиг, поднимает контейнер общих сервисов
(журналы вызовов, реестр запусков, способ запуска, реестр инструментов,
сервис входа), строит endpoint'ы MCP над реестром — по одному на профиль
конфига — и слушает адрес секции [dag].

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
from starlette.routing import Route

from boba.auth import AuthService
from boba.cancellation import StopReason
from boba.connections.sealed import SealKeys
from boba.dag_service.auth import ProxySignInRoute, SessionTokenVerifier
from boba.dag_service.server import DagEndpoints, RunLimits, TokenClaim
from boba.identity.context import CallContexts
from boba.runtime import providers
from boba.runtime.config import AppName, RuntimeConfig
from boba.runtime.di import Container
from boba.runtime.plugins import EntryPointPlugins
from boba.toolkit.types import StringList

__all__ = ["DagAppConfig", "DagEntry", "DagHost", "DagSection", "DagToken"]

logger = logging.getLogger(__name__)


class DagToken(BaseModel):
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


class DagSection(BaseModel):
    """Секция [dag]: адрес сервиса, общий путь endpoint'ов MCP, пределы
    запусков и готовые токены доступа. Endpoint'ы — профили [profiles.*]
    конфига сервиса: `{path}/{профиль}`; вход — секции [auth.*]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    path: str = Field(min_length=1)
    limits: RunLimits
    tokens: dict[str, DagToken]
    """Токены доступа по именам записей конфига."""


class DagAppConfig(RuntimeConfig):
    """Секции процесса сервиса: общие секции приложения и [dag]."""

    dag: DagSection


class DagHost:
    """Сборка процесса сервиса: контейнер общих сервисов и MCP-сервер над
    реестром инструментов. Создаётся входом процесса из конфига."""

    def __init__(self, config: DagAppConfig) -> None:
        self._config = config
        self._container = Container(level="app")
        self._container.provide(providers.get_runtime_config, config)
        self._container.provide(providers.plugin_table, EntryPointPlugins.discover)
        self._container.provide(providers.app_name, AppName.DAG)
        self._container.provide(providers.seal_keys, SealKeys())
        self._container.provide(providers.call_contexts, CallContexts())
        self._container.eager(providers.message_bus)
        self._container.eager(providers.connection_types)
        self._container.eager(providers.call_ambient)
        self._container.eager(providers.runs)
        self._container.eager(providers.call_journals)
        self._container.eager(providers.credential_source)
        self._container.eager(providers.users_table)
        self._container.eager(providers.session_tokens)
        self._container.eager(providers.user_directory)
        self._container.eager(providers.auth_service)
        self._container.eager(providers.tool_launchers)
        self._container.eager(providers.tool_registry)

    async def serve(self) -> None:
        """Поднимает сервисы, слушает адрес секции [dag] до остановки процесса
        и гасит запуски и зиготы на выходе."""
        section = self._config.dag
        Container.set_root(self._container)
        await self._container.start()
        try:
            auth = self._container.resolved(providers.auth_service)
            endpoints = DagEndpoints(
                self._container.resolved(providers.tool_registry),
                self._container.resolved(providers.runs),
                self._container.resolved(providers.call_journals),
                SessionTokenVerifier(auth, self._tokens(section)),
                section.limits,
                section.path,
                self._sign_in_routes(auth),
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
                "boba-dag listens on %s:%d, mcp endpoints: %s",
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
    def _tokens(section: DagSection) -> dict[str, dict[str, object]]:
        tokens: dict[str, dict[str, object]] = {}
        for declared in section.tokens.values():
            tokens[declared.token.get_secret_value()] = declared.claims()

        return tokens

    def _sign_in_routes(self, auth: AuthService) -> list[Route]:
        """Маршруты входа по [auth]: пока подключён вход proxy."""
        routes: list[Route] = []
        if proxy := self._config.proxy():
            routes.append(ProxySignInRoute(proxy, auth).route())

        return routes


class DagEntry:
    """Вход процесса: `boba-dag --config <toml>`."""

    def config_argument(self) -> Path:
        """Путь конфига — обязательный аргумент запуска; дефолта и env нет."""
        parser = argparse.ArgumentParser(
            prog="boba-dag", description="Tool execution service of boba"
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help="path to the service config.toml",
        )

        return parser.parse_args().config

    async def run(self) -> None:
        config = DagAppConfig.load(self.config_argument())
        logging.config.dictConfig(config.logger)

        await DagHost(config).serve()


async def main() -> None:
    try:
        await DagEntry().run()
    except Exception as exc:
        raise SystemExit(f"boba-dag failed: {type(exc).__name__}: {exc}") from exc


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
