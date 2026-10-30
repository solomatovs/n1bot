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

from __future__ import annotations

import argparse
import logging
import logging.config
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Self

import uvicorn
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import ConfigDict, Field, StringConstraints

from boba.auth.credentials import NoRefresh
from boba.auth.proxy import ProxyAssertions
from boba.cancellation import ProcessStop, StopReason
from boba.config import ConfigLoader, bind
from boba.connection_broker.sealed import ServerConnection
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.identity.run import Runs
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
    ServiceAddress,
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
)
from boba.runtime.di import Container
from boba.runtime.http import HealthProbe, SignalledServer
from boba.runtime.launchers import ToolLaunchers
from boba.runtime.storage import LocalStorageConfig, StorageFactory

__all__ = ["McpAppConfig", "McpEntry", "McpHost", "McpSection"]

logger = logging.getLogger(__name__)


class McpSection(ServiceAddress):
    """Секция [mcp]: адрес сервиса и его префикс (ServiceAddress), пределы
    запусков, endpoint'ы и доверенные клиенты. Endpoint
    [mcp.endpoints.<имя>] — набор инструментов (tools), роли, которым он
    доступен (roles), и правила предложения соединений (connections);
    отвечает на `{url_prefix}/{имя}`. Вход — секции [auth.*]."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(gt=0, lt=65536)
    shutdown_timeout_sec: int = Field(gt=0)
    """Сколько остановка ждёт, пока клиенты закроют соединения сами; дальше
    соединения рвутся, и сервис гасит запуски и зиготы. Без срока остановка
    ждала бы keep-alive соединений прокси бесконечно и не доходила до
    завершения запусков."""
    limits: RunLimits
    endpoints: dict[str, McpEndpoint] = Field(min_length=1)
    clients: dict[str, TrustedClient]
    """Доверенные клиенты OAuth по client_id: им разрешён вход proxy."""


class McpAppConfig(ProcessConfig):
    """Секции процесса сервиса: секции процесса, [mcp], хранилище workspace и
    заданные соединения сервера."""

    mcp: McpSection
    storage: LocalStorageConfig
    """Хранилище workspace: туда пишут маршруты файлов и оттуда читают
    инструменты."""
    connections: dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")],
        ServerConnection,
    ]
    """Заданные соединения сервера [connections.<имя>]: имя — идентификатор
    без точек, поэтому его не спутать с запечатанным значением."""

    @classmethod
    def load(cls, paths: Sequence[Path]) -> Self:
        """Конфиг процесса и проверка предпосылок способа запуска инструментов
        [tool_launcher]: отказ виден на старте сервиса."""
        config = super().load(paths)
        ToolLaunchers(RawConfig.get()).build().probe()

        return config


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
        self._container.provide(providers.seal_keys, SealKeys())
        self._container.provide(providers.server_connections, config.connections)
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
        self._container.eager(providers.plugin_kits)
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
                section.endpoints,
                self._container.resolved(providers.connection_params),
                section.limits,
                self._config.storage,
                StorageFactory(self._config.instance).create(self._config.storage),
            )
            stop = ProcessStop()
            stop.on_stop(StoppedRuns(self._container.resolved(providers.runs)))
            stop.on_stop(EndedSubscriptions(endpoints))
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
            ServiceMark.TOKEN_SALT,
        )
        assertions = None
        if proxy := self._config.proxy():
            assertions = ProxyAssertions(
                proxy.secret.get_secret_value(), proxy.max_skew_sec
            )

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
        )

        return ServiceAuth(public_url, server, tokens, ServiceMark.NAME, resources)

    def _clients(self, section: McpSection, sealed: SealedValues) -> RegisteredClients:
        """Клиенты OAuth: доверенные из [mcp.clients] и регистрирующиеся сами."""
        declared: dict[str, OAuthClientInformationFull] = {}
        for client_id, client in section.clients.items():
            declared[client_id] = client.registered(client_id)

        return RegisteredClients(sealed, declared)


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

    async def serve(self, paths: Sequence[Path]) -> None:
        config = McpAppConfig.load(paths)
        logging.config.dictConfig(config.logger)

        await McpHost(config).serve()

    def health(self, paths: Sequence[Path]) -> None:
        """Проба готовности по [mcp]: порт и префикс сервиса."""
        section = bind(ConfigLoader(paths).load(), "mcp", McpSection)
        probe = HealthProbe(section.port, section.url_prefix, McpEndpoints.HEALTH)

        sys.stdout.write(f"{probe.check()}\n")


async def main() -> None:
    try:
        await McpEntry().run()
    except Exception as exc:
        raise SystemExit(f"boba-mcp failed: {type(exc).__name__}: {exc}") from exc
