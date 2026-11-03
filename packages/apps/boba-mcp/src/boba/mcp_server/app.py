"""Процесс сервиса исполнения: сборка объектов и запуск MCP-сервера.

`boba-mcp serve --config <toml> [--config <наложение>]` читает конфиг
экземпляра (промпты endpoint'ов он подключает сам), поднимает контейнер
общих сервисов (журналы вызовов, реестр запусков, способ запуска, реестр
инструментов, способы входа), строит endpoint'ы MCP над реестром — по одному на
профиль конфига — и слушает адрес секции [mcp]. `boba-mcp health --config
<toml>` пробует готовность запущенного сервиса на порту из [mcp].

Ошибки:
SystemExit — конфиг не прочитан, сервис не поднялся либо не ответил на
    пробу готовности; текст называет причину.
"""

import argparse
import logging
import logging.config
import signal
import sys
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Annotated, Self

import uvicorn
from pydantic import BaseModel, ConfigDict, Field

from boba.access import RoleConfig
from boba.auth.credentials import NoRefresh
from boba.auth.proxy import ProxyAssertionVerifier
from boba.cancellation import ProcessStop, StopReason
from boba.config import ConfigSchema, bind
from boba.connection_broker.crypto import SecretCipher
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.manifest import ConnectionTypes
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.identity.service import ServiceAddress
from boba.identity_postgres import UsersTable
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.server import (
    EndpointCatalog,
    McpEndpoint,
    McpEndpoints,
    RunLimits,
)
from boba.oauth.server import (
    AuthServer,
    RegisteredClients,
    SealedValues,
    ServiceAuth,
    ServiceTokens,
    TrustedClient,
)
from boba.runtime import providers
from boba.runtime.config import (
    ConfigArguments,
    ProcessConfig,
    ProgramCall,
    ProgramCommand,
    RawConfig,
    ServerConfig,
    ValueCipherConfig,
)
from boba.runtime.di import Container, Depends
from boba.runtime.http import HealthProbe, SignalledServer
from boba.runtime.launchers import ToolLaunchers
from boba.runtime.storage import StorageConfig, StorageFactory

__all__ = ["McpAppConfig", "McpEntry", "McpHost", "McpSection"]

logger = logging.getLogger(__name__)


class McpSection(BaseModel):
    """Секция [mcp]: адрес сервиса и его префикс (ServiceAddress), пределы
    запусков, endpoint'ы и доверенные клиенты. Endpoint
    [mcp.endpoints.<имя>] — набор инструментов (tools), роли, которым он
    доступен (roles), и правила предложения соединений (connections);
    отвечает на `{url_prefix}/{имя}`. Вход — секции [auth.*]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    """Сколько остановка ждёт, пока клиенты закроют соединения сами; дальше
    соединения рвутся, и сервис гасит запуски и зиготы. Без срока остановка
    ждала бы keep-alive соединений прокси бесконечно и не доходила до
    завершения запусков."""
    limits: RunLimits
    endpoints: dict[str, McpEndpoint] = Field(min_length=1)
    clients: dict[str, TrustedClient] = Field(default_factory=dict)
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class McpServerConfig(ServerConfig, ServiceAddress):
    model_config = ConfigDict(extra="forbid", frozen=True)
    port: int = Field(default=8650, gt=0, lt=65536)


class McpAppConfig(ProcessConfig):
    """Секции процесса сервиса: секции процесса, [mcp], хранилище workspace и
    каталоги соединений и пользователей."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    server: McpServerConfig
    mcp: McpSection
    roles: dict[str, RoleConfig]
    """Права инструментов по ролям; отсутствие гранта запрещает вызов."""
    postgres: PostgresConfig
    connection_store: ValueCipherConfig
    """Ключ шифрования секретов каталога MCP, отдельный от подписи токенов."""
    storage: StorageConfig
    """Хранилище workspace: туда пишут маршруты файлов и оттуда читают
    инструменты."""

    @classmethod
    def load(cls, paths: Sequence[Path]) -> Self:
        """Конфиг процесса и проверка предпосылок способа запуска инструментов
        [tool_launcher]: отказ виден на старте сервиса."""
        config = super().load(paths)
        ToolLaunchers(RawConfig.get()).build().probe()

        return config


async def mcp_pool(
    config: Annotated[McpAppConfig, Depends(providers.get_process_config)],
) -> AsyncGenerator[AsyncPostgresPool, None]:
    """Один пул MCP для пользователей, ролей, соединений и грантов."""
    pool = await AsyncPostgresPool.get(config.postgres)
    try:
        yield pool
    finally:
        await pool.close()


def mcp_users(
    config: Annotated[McpAppConfig, Depends(providers.get_process_config)],
    pool: Annotated[AsyncPostgresPool, Depends(mcp_pool)],
) -> UsersTable:
    """Общая UsersTable в схеме MCP, без таблиц чата."""
    return UsersTable(config.postgres, config.db_schema, pool)


async def mcp_connections(
    config: Annotated[McpAppConfig, Depends(providers.get_process_config)],
    pool: Annotated[AsyncPostgresPool, Depends(mcp_pool)],
    types: Annotated[ConnectionTypes, Depends(providers.connection_types)],
) -> ConnectionStore:
    """Тот же брокер, что у AID, но с данными и ключом экземпляра MCP."""
    cipher = SecretCipher(config.connection_store.cipher.get_secret_value())
    store = ConnectionStore(pool, config.db_schema, cipher, types)
    await store.sync_roles(config.roles)
    return store


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


class EndedSubscriptions:
    """Действие остановки процесса сервиса: штатно кончает потоки
    subscriptions/listen.

    Создаётся сборкой процесса (McpHost) над endpoint'ами и отдаётся
    остановке процесса (ProcessStop.on_stop). Каждый поток досылает события
    и отвечает на свой запрос итогом: клиент видит конец подписки, а не
    обрыв, и серверу не приходится ждать долгие потоки весь срок остановки.
    """

    def __init__(self, endpoints: McpEndpoints) -> None:
        self._endpoints = endpoints

    def __call__(self, received: signal.Signals) -> None:
        self._endpoints.end_subscriptions()
        logger.info("stop signal %s: listen streams are ended", received.name)


class McpHost:
    """Сборка процесса сервиса: контейнер общих сервисов и MCP-сервер над
    реестром инструментов. Создаётся входом процесса из конфига."""

    def __init__(self, config: McpAppConfig) -> None:
        self._config = config
        self._container = Container(level="app")
        self._container.provide(providers.get_process_config, config)
        contexts = CallContexts()
        self._container.provide(providers.call_contexts, contexts)
        resolver = ArmedConnections(
            self._connection_store, providers.credential_source_ref, contexts
        )
        self._container.provide(providers.stored_connections, resolver)
        catalog_tools = ConnectionTools(self._connection_store, contexts).build()
        self._container.provide(providers.own_tools, catalog_tools)
        # сигнал «обнови билет входа» слушает страница чата: у сервиса её нет
        self._container.provide(providers.refresh_signal, NoRefresh())
        # Endpoint ограничивает набор инструментов, роль — права внутри него.
        self._endpoints = EndpointCatalog(config.mcp.endpoints)
        self._container.provide(
            providers.tool_grants, self._endpoints.grants(config.roles)
        )
        self._container.provide(providers.profile_catalog, self._endpoints)
        self._container.eager(providers.connection_types)
        self._container.eager(providers.call_ambient)
        self._container.eager(providers.runs)
        self._container.eager(providers.call_journals)
        self._container.eager(providers.credential_source)
        self._container.eager(providers.user_directory)
        self._container.eager(providers.sign_ins)
        self._container.eager(mcp_users)
        self._container.eager(mcp_connections)
        self._container.eager(providers.tool_launchers)
        self._container.eager(providers.plugin_kits)
        self._container.eager(providers.tool_registry)

    def _connection_store(self) -> ConnectionStore:
        return self._container.resolved(mcp_connections)

    async def serve(self) -> None:
        """Поднимает сервисы, слушает адрес секции [mcp] до остановки процесса
        и гасит запуски и зиготы на выходе."""
        section = self._config.mcp
        server = self._config.server
        Container.set_root(self._container)
        await self._container.start()
        try:
            auth = self._auth(section)
            endpoints = McpEndpoints(
                self._container.resolved(providers.tool_registry),
                self._container.resolved(providers.runs),
                self._container.resolved(providers.call_journals),
                auth,
                section.endpoints,
                self._container.resolved(providers.connection_params),
                section.limits,
                self._config.storage,
                StorageFactory(self._config.paths).create(self._config.storage),
            )
            stop = ProcessStop()
            stop.on_stop(StoppedRuns(self._container.resolved(providers.runs)))
            stop.on_stop(EndedSubscriptions(endpoints))
            listener = SignalledServer(
                uvicorn.Config(
                    endpoints.app(),
                    host=server.host,
                    port=server.port,
                    log_config=None,
                    timeout_graceful_shutdown=server.shutdown_timeout_sec,
                ),
                stop,
            )
            logger.info(
                "boba-mcp listens on %s:%d, mcp endpoints: %s",
                server.host,
                server.port,
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
        public_url = self._config.server.public_url.rstrip("/")
        generation = session.session_generation()
        logger.info("session generation of this process: %s", generation)
        tokens = ServiceTokens(
            public_url,
            session.auth_secret,
            session.session_ttl_sec,
            session.session_max_sec,
            generation,
            ServiceMark.TOKEN_SALT,
        )
        assertions = ProxyAssertionVerifier(self._config.proxy_entries())

        sealed = SealedValues(session.auth_secret, ServiceMark.SEALED_SALT)
        resources: dict[str, str] = {}
        for name in section.endpoints:
            resources[name] = f"{public_url}/{name}"

        server = AuthServer(
            public_url,
            self._container.resolved(providers.sign_ins),
            tokens,
            assertions,
            self._clients(section, sealed),
            sealed,
            list(resources.values()),
            users=self._container.resolved(mcp_users),
        )

        return ServiceAuth(public_url, server, tokens, ServiceMark.NAME, resources)

    def _clients(self, section: McpSection, sealed: SealedValues) -> RegisteredClients:
        """Клиенты OAuth: доверенные из [mcp.clients] и регистрирующиеся сами."""
        return RegisteredClients(sealed, section.clients)


class McpEntry:
    """Вход процесса: `boba-mcp serve|health --config <toml> [--config <наложение>]`."""

    def call(self) -> ProgramCall:
        """Подкоманда и файлы конфига: конфиг экземпляра и наложения."""
        parser = argparse.ArgumentParser(
            prog="boba-mcp", description="Tool execution service of boba"
        )

        return ConfigArguments(parser).call()

    async def run(self) -> None:
        call = self.call()
        match call.command:
            case ProgramCommand.SERVE:
                await self.serve(call.paths)
            case ProgramCommand.HEALTH:
                self.health(call.paths)
            case ProgramCommand.MIGRATE:
                from boba.runtime.migrate import (  # noqa: PLC0415 — служебный режим
                    ApplicationMigrations,
                )
                from boba.runtime.plugins import PluginMigrations  # noqa: PLC0415

                sources = PluginMigrations()
                migration = ApplicationMigrations(
                    (sources,), schema=ConfigSchema(McpAppConfig, values=("auth",))
                )
                await migration.run(
                    call.paths,
                    (
                        "boba.identity_postgres",
                        "boba.access_postgres",
                        "boba.connection_broker",
                    ),
                )

    async def serve(self, paths: Sequence[Path]) -> None:
        config = McpAppConfig.load(paths)
        logging.config.dictConfig(config.logger)

        await McpHost(config).serve()

    def health(self, paths: Sequence[Path]) -> None:
        """Проба готовности по [mcp]: порт и префикс сервиса."""
        section = bind(McpAppConfig.loader(paths).load(), "server", McpServerConfig)
        probe = HealthProbe(section.port, section.url_prefix, McpEndpoints.HEALTH)

        sys.stdout.write(f"{probe.check()}\n")


async def main() -> None:
    try:
        await McpEntry().run()
    except Exception as exc:
        raise SystemExit(f"boba-mcp failed: {type(exc).__name__}: {exc}") from exc
