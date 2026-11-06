"""Процесс MCP: плагины из TOML, endpoint'ы и grants из PostgreSQL.

Ошибки:
SystemExit — конфиг, миграция, запуск службы или проба готовности отказали;
    сообщение сохраняет причину и контекст компонента.
"""

import argparse
import json
import logging
import logging.config
import signal
import sys
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import uvicorn
from fastmcp.tools import Tool
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator

from boba.auth import SessionService, SignIns
from boba.auth.assembly import AuthBackends, AuthServices
from boba.auth.config import AuthConfig, ProxyAuthConfig
from boba.auth.providers.backends import KerberosSessionTickets, StandardAuthBackends
from boba.auth.settings import SignInSettings
from boba.cancellation import ProcessStop, StopReason
from boba.config import ConfigVariant, bind
from boba.config.application import ConfigFiles
from boba.config.arguments import CommandSpec, ConfigArguments
from boba.config.paths import PathsConfig, TlsConfig
from boba.connections.manifest import ConnectionTypes
from boba.crypto import SecretCipher
from boba.crypto.config import ValueCipherConfig
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migration_options import MigrationArguments, MigrationOptions
from boba.http_server import HealthProbe, ProcessLogging, ServerConfig, SignalledServer
from boba.identity.context import CallContexts
from boba.identity.run import Runs
from boba.identity.service import ServiceAddress
from boba.identity_postgres import UsersTable
from boba.krb.connections import KerberosCredentialSource
from boba.krb.layout import InstanceLayout
from boba.ldap import Ldap3Directory
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.catalog import EndpointCatalog
from boba.mcp_server.connections import NoRefresh
from boba.mcp_server.connections.params import ConnectionParams
from boba.mcp_server.connections.store import ConnectionStore
from boba.mcp_server.connections.tools import ConnectionTools
from boba.mcp_server.connections.user_connections import ArmedConnections
from boba.mcp_server.endpoints import McpEndpointStore
from boba.mcp_server.reload import ReloadableMcp
from boba.mcp_server.schemas import EndpointSchemas
from boba.mcp_server.server import (
    FileUploadSchema,
    McpEndpoints,
    RunLimitMiddleware,
    RunLimits,
)
from boba.mcp_server.storage import StandardStorageBackends
from boba.mcp_server.tool_adapter import McpTool
from boba.oauth.server.assertions import ProxyAssertionVerifier
from boba.oauth.server.clients import RegisteredClients, TrustedClient
from boba.oauth.server.config import TokenConfig
from boba.oauth.server.provider import AuthServer
from boba.oauth.server.resources import ServiceAuth
from boba.oauth.server.sealed import SealedValues
from boba.oauth.server.tokens import ServiceTokens
from boba.storage.client import StorageConfig, StorageFactory
from boba.storage.journal import DirVault, StreamJournal
from boba.storage.journal_config import StreamJournalConfig
from boba.tool_io.chain import CallAmbient
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.injected import InjectedConfig
from boba.toolrun.launchers import ToolLaunchers
from boba.toolrun.plugins import PluginKits, PluginSections, ToolExecution, ToolLoader
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.sandbox import StandardToolLaunchers
from boba.toolrun.streams import CallJournals
from boba.toolrun.tickets import SectionTickets, ServiceTickets

__all__ = ["McpAppConfig", "McpEntry", "McpHost", "McpSection"]

logger = logging.getLogger(__name__)


class McpSection(BaseModel):
    """Пределы запусков MCP и доверенные OAuth-клиенты из конфига процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limits: RunLimits
    clients: dict[str, TrustedClient] = Field(default_factory=dict)
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class McpServerConfig(ServerConfig, ServiceAddress):
    model_config = ConfigDict(extra="forbid", frozen=True)
    port: int = Field(default=8650, gt=0, lt=65536)


class McpAppConfig(SignInSettings):
    """Секции процесса сервиса: секции процесса, [mcp], хранилище workspace и
    каталоги соединений и пользователей."""

    db_schema: str = Field(alias="schema", min_length=1)
    plugins: str = ""
    paths: PathsConfig = Field(alias="data")
    tls: TlsConfig = Field(default_factory=TlsConfig)
    session: TokenConfig
    logger: dict[str, Any] = Field(default_factory=ProcessLogging().default)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    AUTH_BACKENDS: ClassVar[AuthBackends] = StandardAuthBackends().registry
    LAUNCHER_BACKENDS: ClassVar = StandardToolLaunchers().registry
    CONFIG_FILES: ClassVar = PluginSections.FILE_SECTIONS

    server: McpServerConfig
    mcp: McpSection
    postgres: PostgresConfig
    connection_store: ValueCipherConfig
    """Ключ шифрования секретов каталога MCP, отдельный от подписи токенов."""
    STORAGE_BACKENDS: ClassVar = StandardStorageBackends().registry
    CONFIG_VARIANTS: ClassVar = {
        AuthConfig: ConfigVariant("type", AUTH_BACKENDS.model_for),
        StorageConfig: ConfigVariant("kind", STORAGE_BACKENDS.model_for),
    }

    storage: SerializeAsAny[StorageConfig]
    """Хранилище workspace: туда пишут маршруты файлов и оттуда читают
    инструменты."""

    @field_validator("storage", mode="before")
    @classmethod
    def _storage_backend(cls, raw: object) -> StorageConfig:
        return cls.STORAGE_BACKENDS.parse(raw)


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

    def __init__(self, endpoints: ReloadableMcp) -> None:
        self._endpoints = endpoints

    def __call__(self, received: signal.Signals) -> None:
        self._endpoints.begin_shutdown()
        logger.info("stop signal %s: listen streams are ended", received.name)


@dataclass(frozen=True)
class McpRuntime:
    """Ресурсы запущенного MCP; срок жизни ограничен контекстом McpHost."""

    catalog: EndpointCatalog
    endpoints: McpEndpointStore
    users: UsersTable
    sign_ins: SignIns
    runs: Runs
    journals: CallJournals
    params: ConnectionParams
    tools: ToolRegistry


class McpHost:
    """Собирает MCP из его конфига и закрывает ресурсы в обратном порядке."""

    def __init__(self, config: McpAppConfig, raw: DictConfig) -> None:
        self._config = config
        self._raw = raw
        self._types = ConnectionTypes.discover()
        self._directory = Ldap3Directory()
        self._contexts = CallContexts()
        self._ambient = CallAmbient()
        self._launchers = ToolLaunchers(raw, config.LAUNCHER_BACKENDS)
        self._storage = StorageFactory(config.paths, config.STORAGE_BACKENDS)
        self._schemas = EndpointSchemas()
        self._upload_schema = FileUploadSchema()
        self._tickets = KerberosSessionTickets()
        self._cipher = SecretCipher(config.connection_store.cipher.get_secret_value())

    @asynccontextmanager
    async def _running(self) -> AsyncGenerator[McpRuntime, None]:
        async with AsyncExitStack() as cleanup:
            pool = await AsyncPostgresPool.get(self._config.postgres)
            cleanup.push_async_callback(pool.close)
            users = UsersTable(pool, self._config.db_schema)
            store = ConnectionStore(
                pool, self._config.db_schema, self._cipher, self._types
            )
            endpoints = McpEndpointStore(pool, self._config.db_schema)
            catalog = EndpointCatalog(())
            auth_services = AuthServices(
                self._directory, catalog, users, self._config.session.auth_secret
            )
            sign_ins = self._config.AUTH_BACKENDS.build(
                self._config.auth, auth_services
            )
            runs = Runs(self._contexts)
            journals = self._journals(runs)
            credentials = KerberosCredentialSource(
                self._tickets.build(
                    self._config.auth, self._config.session.auth_secret
                ),
                NoRefresh(),
            )
            tickets = ServiceTickets(credentials)
            resolver = ArmedConnections(store, credentials, self._contexts)
            params = ConnectionParams(self._types, self._contexts, tickets, resolver)
            connection_tools = ConnectionTools(store, self._contexts)
            own = connection_tools.build()
            execution = ToolExecution(
                self._contexts,
                journals,
                self._ambient,
                (params, InjectedConfig(SectionTickets(tickets, self._types))),
                (params,),
            )
            launchers = self._launchers.build()
            cleanup.callback(launchers.stop)
            cleanup.callback(runs.stop_all, StopReason.SHUTDOWN)
            launchers.probe()
            plugins = PluginKits(self._raw, launchers)
            kits = await plugins.load()
            loader = ToolLoader(self._raw, kits, execution, launchers, catalog, own)
            tools = loader.load()

            yield McpRuntime(
                catalog,
                endpoints,
                users,
                sign_ins,
                runs,
                journals,
                params,
                tools,
            )

    def _journals(self, runs: Runs) -> CallJournals:
        config = bind(self._raw, StreamJournalConfig.SECTION, StreamJournalConfig)
        if not config.enable:
            return CallJournals(None, runs)

        vault = DirVault(str(self._config.paths.data("tool-logs")))

        return CallJournals(StreamJournal(vault, config.reserve_bytes), runs)

    async def serve(self) -> None:
        """Собирает опубликованные endpoint'ы и слушает адрес до остановки процесса."""
        section = self._config.mcp
        server = self._config.server
        async with self._running() as runtime:
            limits = RunLimitMiddleware(section.limits, McpEndpoints.OPERATIONS)
            files = self._storage.create(self._config.storage)

            def build(catalog: EndpointCatalog) -> McpEndpoints:
                return McpEndpoints(
                    runtime.tools,
                    runtime.runs,
                    runtime.journals,
                    self._auth(section, catalog, runtime),
                    catalog.endpoints(),
                    runtime.params,
                    limits,
                    self._config.storage,
                    files,
                )

            endpoints = ReloadableMcp(runtime.endpoints, runtime.catalog, build)
            stop = ProcessStop()
            stop.on_stop(StoppedRuns(runtime.runs))
            stop.on_stop(EndedSubscriptions(endpoints))
            stop.signals.on_async(signal.SIGHUP, endpoints.on_reload)
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
                "boba-mcp starting on %s:%d; SIGHUP reloads endpoint definitions",
                server.host,
                server.port,
            )
            await listener.serve_until_stopped()

    async def tool_schemas(self) -> Mapping[str, Mapping[str, object]]:
        """Схемы всех загруженных инструментов для администратора, без HTTP-сервера."""
        async with self._running() as runtime:
            published = runtime.params.published(runtime.tools.tools)
            calls = DirectCalls()
            port = runtime.tools.server(published, calls)
            tools: dict[str, Tool] = {}
            for card in port.tools():
                tools[card.name] = McpTool(card, port)

            upload = self._upload_schema.tool
            tools[upload.name] = upload

            return self._schemas.export(tools)

    def _auth(
        self, section: McpSection, catalog: EndpointCatalog, runtime: McpRuntime
    ) -> ServiceAuth:
        """Вход сервиса: сервер авторизации над способами входа [auth] и
        проверяющие токена endpoint'ов."""
        session = self._config.session
        public_url = self._config.server.public_url.rstrip("/")
        generation = session.session_generation()
        logger.info("session generation of this process: %s", generation)
        tokens = ServiceTokens(
            public_url,
            session.auth_secret,
            session.lifetime(),
            generation,
            ServiceMark.TOKEN_SALT,
        )
        assertions = ProxyAssertionVerifier(self._config.auth_entries(ProxyAuthConfig))

        sealed = SealedValues(session.auth_secret, ServiceMark.SEALED_SALT)
        resources = catalog.resources(public_url)

        server = AuthServer(
            public_url,
            runtime.sign_ins,
            tokens,
            assertions,
            self._clients(section, sealed),
            sealed,
            list(resources.values()),
            sessions=SessionService(runtime.users, runtime.catalog),
        )

        return ServiceAuth(
            public_url,
            server,
            tokens,
            ServiceMark.NAME,
            resources,
            profiles=runtime.catalog,
        )

    def _clients(self, section: McpSection, sealed: SealedValues) -> RegisteredClients:
        """Клиенты OAuth: доверенные из [mcp.clients] и регистрирующиеся сами."""
        return RegisteredClients(sealed, section.clients)


class McpCommand(StrEnum):
    SERVE = "serve"
    HEALTH = "health"
    MIGRATE = "migrate"
    TOOLS = "tools"


@dataclass(frozen=True)
class McpCall:
    command: McpCommand
    paths: tuple[Path, ...]
    migration: MigrationOptions = field(default_factory=MigrationOptions)


class McpEntry:
    """Консоль MCP: сервер, готовность, миграции и экспорт схем инструментов."""

    def __init__(self) -> None:
        self._migration = MigrationArguments()
        self._configs = ConfigFiles(McpAppConfig, values=("auth",))

    def call(self) -> McpCall:
        parser = argparse.ArgumentParser(
            prog="boba-mcp", description="Tool execution service of boba"
        )
        commands = (
            CommandSpec(McpCommand.SERVE, "run the MCP service"),
            CommandSpec(McpCommand.HEALTH, "probe the MCP service"),
            CommandSpec(McpCommand.TOOLS, "export tool schemas"),
            CommandSpec(
                McpCommand.MIGRATE, "apply MCP and plugin schemas", self._migration.add
            ),
        )
        arguments = ConfigArguments(parser, commands)
        call = arguments.call()
        command = McpCommand(call.command)
        migration = MigrationOptions()
        if command is McpCommand.MIGRATE:
            migration = self._migration.read(call.arguments)

        return McpCall(command=command, paths=call.paths, migration=migration)

    async def run(self) -> None:
        call = self.call()
        match call.command:
            case McpCommand.SERVE:
                await self.serve(call.paths)
            case McpCommand.HEALTH:
                self.health(call.paths)
            case McpCommand.TOOLS:
                await self.tools(call.paths)
            case McpCommand.MIGRATE:
                await self.migrate(call.paths, call.migration)

    async def tools(self, paths: Sequence[Path]) -> None:
        loaded = self._configs.load(paths)
        config = loaded.value
        layout = InstanceLayout(paths[0].resolve().parent, config.paths, config.tls)
        layout.apply()
        logging.config.dictConfig(config.logger)

        host = McpHost(config, loaded.raw)
        schemas = await host.tool_schemas()
        sys.stdout.write(json.dumps(schemas, ensure_ascii=False, indent=2) + "\n")

    async def migrate(self, paths: Sequence[Path], options: MigrationOptions) -> None:
        from boba.mcp_server.migrations import configured_migrations  # noqa: PLC0415

        migrations = configured_migrations()
        await migrations.run(paths, "boba.mcp_server", options=options)

    async def serve(self, paths: Sequence[Path]) -> None:
        loaded = self._configs.load(paths)
        config = loaded.value
        layout = InstanceLayout(paths[0].resolve().parent, config.paths, config.tls)
        layout.apply()
        logging.config.dictConfig(config.logger)

        host = McpHost(config, loaded.raw)
        await host.serve()

    def health(self, paths: Sequence[Path]) -> None:
        """Проба готовности по [mcp]: порт и префикс сервиса."""
        section = bind(self._configs.read(paths), "server", McpServerConfig)
        probe = HealthProbe(section.port, section.url_prefix, McpEndpoints.HEALTH)

        sys.stdout.write(f"{probe.check()}\n")


async def main() -> None:
    try:
        entry = McpEntry()
        await entry.run()
    except Exception as exc:
        raise SystemExit(f"boba-mcp failed: {type(exc).__name__}: {exc}") from exc
