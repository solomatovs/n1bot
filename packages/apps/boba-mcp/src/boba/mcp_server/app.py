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
import signal
from pathlib import Path

import uvicorn
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import BaseModel, ConfigDict, Field

from boba.access import ProfileGrant
from boba.auth.credentials import NoRefresh
from boba.auth.proxy import ProxyAssertions
from boba.cancellation import ProcessStop, StopReason
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.mcp_server.auth import (
    AuthServer,
    McpClient,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
)
from boba.mcp_server.server import (
    EndpointCatalog,
    McpEndpoints,
    RunLimits,
)
from boba.runtime import providers
from boba.runtime.config import ProcessConfig
from boba.runtime.di import Container
from boba.runtime.http import SignalledServer
from boba.runtime.plugins import EntryPointPlugins
from boba.runtime.storage import LocalStorageConfig

__all__ = ["McpAppConfig", "McpEntry", "McpHost", "McpSection"]

logger = logging.getLogger(__name__)


class McpSection(BaseModel):
    """Секция [mcp]: адрес сервиса, общий путь endpoint'ов MCP, пределы
    запусков, endpoint'ы и доверенные клиенты. Endpoint
    [mcp.endpoints.<имя>] — набор инструментов (tools) и роли, которым он
    доступен (roles); отвечает на `{path}/{имя}`. Вход — секции [auth.*]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    path: str = Field(min_length=1)
    shutdown_timeout_sec: int = Field(gt=0)
    """Сколько остановка ждёт, пока клиенты закроют соединения сами; дальше
    соединения рвутся, и сервис гасит запуски и зиготы. Без срока остановка
    ждала бы keep-alive соединений прокси бесконечно и не доходила до
    завершения запусков."""
    limits: RunLimits
    public_url: str = Field(min_length=1)
    """Адрес сервиса, каким его видят клиенты (за nginx — с префиксом): от
    него строятся метаданные OAuth, адреса входа и издатель токена. Вне
    localhost — только https."""
    endpoints: dict[str, ProfileGrant] = Field(min_length=1)
    clients: dict[str, McpClient]
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class McpAppConfig(ProcessConfig):
    """Секции процесса сервиса: секции процесса, [mcp] и хранилище workspace."""

    mcp: McpSection
    storage: LocalStorageConfig
    """Хранилище workspace: туда пишут маршруты файлов и оттуда читают
    инструменты."""


class StoppedRuns:
    """Действие остановки процесса сервиса: гасит идущие запуски.

    Создаётся сборкой процесса (McpHost) над реестром запусков и отдаётся
    остановке процесса (ProcessStop.on_stop). Вызовы инструментов
    завершаются отказом «остановлен» с причиной SHUTDOWN, их запросы
    отвечают клиентам, и серверу остаётся закрыть простаивающие соединения.
    Без этого uvicorn ждал бы идущие вызовы весь срок остановки и обрывал их
    отменой задач.
    """

    def __init__(self, runs: Runs) -> None:
        self._runs = runs

    def __call__(self, received: signal.Signals) -> None:
        stopped = self._runs.stop_all(StopReason.SHUTDOWN)
        logger.info(
            "stop signal %s: stopping %d running call(s), then the server",
            received.name,
            stopped,
        )


class McpHost:
    """Сборка процесса сервиса: контейнер общих сервисов и MCP-сервер над
    реестром инструментов. Создаётся входом процесса из конфига."""

    def __init__(self, config: McpAppConfig) -> None:
        self._config = config
        self._container = Container(level="app")
        self._container.provide(providers.get_process_config, config)
        self._container.provide(providers.plugin_table, EntryPointPlugins().discover)
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
            stop = ProcessStop()
            stop.on_stop(StoppedRuns(self._container.resolved(providers.runs)))
            listener = SignalledServer(
                uvicorn.Config(
                    endpoints.app(),
                    host=section.host,
                    port=section.port,
                    log_config=None,
                    timeout_graceful_shutdown=section.shutdown_timeout_sec,
                ),
                stop,
            )
            logger.info(
                "boba-mcp listens on %s:%d, mcp endpoints: %s",
                section.host,
                section.port,
                ", ".join(endpoints.paths()),
            )
            await listener.serve_until_stopped()
        finally:
            self._container.resolved(providers.runs).stop_all(StopReason.SHUTDOWN)
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
        resources: dict[str, str] = {}
        for name in section.endpoints:
            resources[name] = f"{public_url}{section.path}/{name}"

        server = AuthServer(
            public_url,
            self._container.resolved(providers.sign_ins),
            tokens,
            assertions,
            self._clients(section, sealed),
            sealed,
            list(resources.values()),
        )

        return ServiceAuth(public_url, server, tokens, resources)

    def _clients(self, section: McpSection, sealed: SealedValues) -> RegisteredClients:
        """Клиенты OAuth: доверенные из [mcp.clients] и регистрирующиеся сами."""
        declared: dict[str, OAuthClientInformationFull] = {}
        for client_id, client in section.clients.items():
            declared[client_id] = client.registered(client_id)

        return RegisteredClients(sealed, declared)


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
