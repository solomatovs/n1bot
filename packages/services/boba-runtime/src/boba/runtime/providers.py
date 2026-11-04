"""Общие провайдеры процессов приложения: конфиг, сторы, реестр инструментов, входы.

Заглушки (get_runtime_config, instance_name) кладёт процесс через provide.
Процесс без браузера (boba-mcp) кладёт вместо get_runtime_config свой
get_process_config: провайдеры ядра читают только секции ProcessConfig.

Ошибки:
RuntimeError — контейнер не поднят или процесс не дал значение.
"""

import logging
from collections.abc import AsyncGenerator, Generator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Annotated

from omegaconf import DictConfig

from boba.access import ToolPolicy
from boba.auth import AuthService, JwtTokens, SignIns
from boba.auth.assembly import AuthServices
from boba.chat.profiles import ChatProfiles
from boba.config import bind
from boba.connection_broker.credentials import KerberosCredentialSource
from boba.connection_broker.params import ConnectionParams
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import ServiceTickets
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.manifest import ConnectionTypes
from boba.core.components import Components
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContexts
from boba.identity.directory import UserDirectory
from boba.identity.locks import LiveLocks
from boba.identity.run import Runs
from boba.identity.signin import ProfileCatalog
from boba.identity.sso import RefreshSignal
from boba.identity_postgres import UsersTable as IdentityUsers
from boba.ldap import Ldap3Directory
from boba.messaging import (
    MessageBus,
    MessageCodec,
    PayloadStore,
)
from boba.runtime.auth_backends import KerberosSessionTickets
from boba.runtime.commands import CommandRunner
from boba.runtime.config import (
    AppName,
    InstanceData,
    MessagingConfig,
    PathsConfig,
    ProcessConfig,
    RawConfig,
    RuntimeConfig,
    StreamJournalConfig,
)
from boba.runtime.di import Container, Depends
from boba.runtime.journal import DirVault, StreamJournal
from boba.runtime.launchers import SectionLaunchers, ToolLaunchers
from boba.runtime.messaging import (
    MessagingRuntime,
    MessagingServices,
    StaleObserverFactory,
)
from boba.runtime.plugins import PluginKit, PluginKits, ToolLoader
from boba.runtime.refresh import BusRefreshSignal, LiveSessions, SessionKeeper
from boba.runtime.refs import ExecRefs
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.tool_io.chain import CallAmbient
from boba.toolrun.hosted import HostedTool
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.streams import CallJournals

logger = logging.getLogger(__name__)


def get_raw_config() -> DictConfig:
    return RawConfig.get()


def get_runtime_config() -> RuntimeConfig:
    """Кладёт процесс после RuntimeConfig.load."""
    msg = (
        "DI provider get_runtime_config resolved before the process supplied "
        "RuntimeConfig via Container.provide"
    )
    raise RuntimeError(msg)


def get_process_config(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> ProcessConfig:
    """Секции процесса для провайдеров ядра: у приложения с браузером — его
    RuntimeConfig; процесс без браузера кладёт свой конфиг через provide."""
    return config


def app_name() -> AppName:
    """Какое приложение поднимает процесс; кладёт процесс через provide."""
    msg = (
        "DI provider app_name resolved before the process supplied AppName "
        "via Container.provide"
    )
    raise RuntimeError(msg)


def instance_name(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    app: Annotated[AppName, Depends(app_name)],
) -> str:
    """Имя инстанса: узел из [cluster] плюс имя приложения."""
    return config.cluster.instance_of(app)


async def postgres_pool(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> AsyncGenerator[AsyncPostgresPool, None]:
    """Пул процесса по [postgres]: открывается один раз в корне процесса,
    его получают все таблицы приложения; закрывается с контейнером."""
    pool = await AsyncPostgresPool.get(config.postgres)
    try:
        async with pool.connection():
            pass

        yield pool
    finally:
        await pool.close()


def message_codec() -> MessageCodec:
    """Каталог сообщений предоставляет корень приложения."""
    raise RuntimeError("the application must provide its message codec")


def messaging_backends() -> Components[
    MessagingConfig, MessagingServices, AbstractAsyncContextManager[MessagingRuntime]
]:
    """Реестр транспортов предоставляет корень приложения."""
    raise RuntimeError("the application must provide its messaging backends")


def stale_observer() -> StaleObserverFactory:
    """Обработчик потерянных держателей предоставляет приложение."""
    raise RuntimeError("the application must provide its stale-lock observer")


async def messaging_runtime(  # noqa: PLR0913 — зависимости ресурса DI
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    instance: Annotated[str, Depends(instance_name)],
    app: Annotated[AppName, Depends(app_name)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
    codec: Annotated[MessageCodec, Depends(message_codec)],
    observer: Annotated[StaleObserverFactory, Depends(stale_observer)],
    backends: Annotated[
        Components[
            MessagingConfig,
            MessagingServices,
            AbstractAsyncContextManager[MessagingRuntime],
        ],
        Depends(messaging_backends),
    ],
) -> AsyncGenerator[MessagingRuntime, None]:
    services = MessagingServices(config, instance, app, pool, codec, observer)
    async with backends.build(config.messaging, services) as runtime:
        yield runtime


def message_bus(
    runtime: Annotated[MessagingRuntime, Depends(messaging_runtime)],
) -> MessageBus:
    return runtime.bus


def payload_store(
    runtime: Annotated[MessagingRuntime, Depends(messaging_runtime)],
) -> PayloadStore:
    return runtime.payloads


def call_contexts() -> CallContexts:
    """Держатель контекста вызова процесса; кладёт процесс: он один на всех,
    кто ставит и читает контекст."""
    msg = (
        "DI provider call_contexts resolved before the process supplied "
        "CallContexts via Container.provide"
    )
    raise RuntimeError(msg)


def refresh_signal(
    contexts: Annotated[CallContexts, Depends(call_contexts)],
) -> RefreshSignal:
    """Сигнал обновления билета входа: в область пользователя через шину процесса."""
    return BusRefreshSignal(message_bus_ref, contexts)


def user_directory() -> UserDirectory:
    """Каталог пользователей процесса: AD через ldap3."""
    return Ldap3Directory()


def live_sessions() -> LiveSessions:
    """Живые сессии инстанса; заглушку заменяет процесс через provide."""
    msg = (
        "DI provider live_sessions resolved before the process supplied "
        "LiveSessions via Container.provide"
    )
    raise RuntimeError(msg)


def own_tools() -> Sequence[HostedTool]:
    """Собственные инструменты процесса — отдельный сервер за портом
    инструментов; у процесса без таких инструментов их нет."""
    return ()


def _root() -> Container:
    return Container.require_root("runtime providers")


def credential_source(
    config: Annotated[ProcessConfig, Depends(get_process_config)],
    refresh: Annotated[RefreshSignal, Depends(refresh_signal)],
) -> KerberosCredentialSource:
    """Источник кредов вызова на процесс; без kerberos в [auth] делегирование
    отказывает.
    """
    tickets = KerberosSessionTickets().build(config.auth, config.session.auth_secret)
    return KerberosCredentialSource(tickets, refresh)


def credential_source_ref() -> KerberosCredentialSource:
    """Источник кредов вызова из корневого контейнера; зовётся на каждый вызов."""
    return _root().resolved(credential_source)


def message_bus_ref() -> MessageBus:
    """Шина процесса для обвязок инструментов; зовётся на каждый вызов."""
    return _root().resolved(message_bus)


def connection_store_ref() -> ConnectionStore:
    """Хранилище соединений для обвязок инструментов; зовётся на каждый вызов."""
    return _root().resolved(connection_store)


def call_contexts_ref() -> CallContexts:
    """Держатель контекста вызова из корневого контейнера."""
    return _root().resolved(call_contexts)


def runs_ref() -> Runs:
    """Реестр идущих запусков из корневого контейнера."""
    return _root().resolved(runs)


def call_journals_ref() -> CallJournals:
    """Журналы живого вывода из корневого контейнера."""
    return _root().resolved(call_journals)


def connection_types_ref() -> ConnectionTypes:
    """Реестр типов соединений; зовётся на запрос."""
    return _root().resolved(connection_types)


def stored_connections() -> ArmedConnections | None:
    """Resolver соединений хранилища; владелец каталога задаёт его явно."""
    return None


def connection_params(
    contexts: Annotated[CallContexts, Depends(call_contexts)],
    stored: Annotated[ArmedConnections | None, Depends(stored_connections)],
) -> ConnectionParams:
    """Соединения инструментов процесса: каталог MCP с проверкой grants."""
    tickets = ServiceTickets(credential_source_ref)

    return ConnectionParams(connection_types_ref, contexts, tickets, stored)


def exec_refs(params: ConnectionParams) -> ExecRefs:
    """Входы исполнения инструментов: ссылки в корневой контейнер и
    соединения инструментов params."""
    return ExecRefs(
        connection_types=connection_types_ref,
        credentials=credential_source_ref,
        contexts=_root().resolved(call_contexts),
        journals=_root().resolved(call_journals),
        ambient=_root().resolved(call_ambient),
        connection_params=params,
    )


def tool_launchers(
    config: Annotated[ProcessConfig, Depends(get_process_config)],
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> Generator[SectionLaunchers, None, None]:
    """Способ запуска инструментов процесса: один на все сборки реестра;
    зиготы гасятся при закрытии контейнера, в том числе после сорванного старта."""
    launchers = ToolLaunchers(raw, config.LAUNCHER_BACKENDS).build()

    try:
        yield launchers
    finally:
        launchers.stop()


def tool_policy() -> ToolPolicy:
    """Приложение передаёт runtime своё решение о доступе к инструментам."""
    msg = "tool policy must be supplied by the application"
    raise RuntimeError(msg)


def profile_catalog(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> ProfileCatalog:
    """Профили приложения для провайдеров профилей входа: по умолчанию профили
    чата из [profiles]; приложение без них отдаёт свой каталог через provide."""
    return ChatProfiles(config.profiles)


async def plugin_kits(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
    launchers: Annotated[SectionLaunchers, Depends(tool_launchers)],
) -> Mapping[str, PluginKit]:
    """Включённые плагины процесса: манифесты от их программ и готовые
    хранилища. Процесс без [tool.*] секций плагинов не имеет."""
    return await PluginKits(raw, launchers).load()


def tool_registry(  # noqa: PLR0913 — реестр собирается всеми входами процесса
    raw: Annotated[DictConfig, Depends(get_raw_config)],
    kits: Annotated[Mapping[str, PluginKit], Depends(plugin_kits)],
    policy: Annotated[ToolPolicy, Depends(tool_policy)],
    own: Annotated[Sequence[HostedTool], Depends(own_tools)],
    launchers: Annotated[SectionLaunchers, Depends(tool_launchers)],
    params: Annotated[ConnectionParams, Depends(connection_params)],
) -> ToolRegistry:
    loader = ToolLoader(raw, kits, exec_refs(params), launchers, policy, own)

    return loader.load()


def live_locks(
    runtime: Annotated[MessagingRuntime, Depends(messaging_runtime)],
) -> LiveLocks:
    return runtime.locks


def connection_types() -> ConnectionTypes:
    """Реестр установленных типов соединений: entry points boba.connections."""
    return ConnectionTypes.discover()


def connection_store() -> ConnectionStore:
    """Каталог connections предоставляет сборка MCP-сервера."""
    raise RuntimeError("connection_store must be supplied by the MCP application")


def call_ambient() -> CallAmbient:
    """Обстановка вызова инструмента процесса: одна на всех, кто её ставит
    и читает."""
    return CallAmbient()


def runs(
    contexts: Annotated[CallContexts, Depends(call_contexts)],
) -> Runs:
    """Реестр идущих запусков процесса."""
    return Runs(contexts)


def call_journals(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
    active: Annotated[Runs, Depends(runs)],
) -> CallJournals:
    """Журналы живого вывода инструментов на процесс по секции [stream_journal];
    выключенный журнал — потоков нет. Процесс без журналов (чат) кладёт свои
    через provide."""
    section = bind(raw, StreamJournalConfig.SECTION, StreamJournalConfig)
    if not section.enable:
        return CallJournals(None, active)

    instance = bind(raw, PathsConfig.SECTION, PathsConfig)
    vault = DirVault(str(instance.data(InstanceData.TOOL_LOGS)))
    return CallJournals(StreamJournal(vault, section.reserve_bytes), active)


async def users_table(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
) -> UsersTable:
    """Строки users и авторы тредов в схеме приложения на пуле процесса."""
    table = UsersTable(config.postgres, config.db_schema, pool)
    return table


def threads_table(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
) -> ThreadsTable:
    """Строки threads в схеме приложения на пуле процесса."""
    return ThreadsTable(config.postgres, config.db_schema, pool)


def session_tokens(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> JwtTokens:
    """Выпуск и чтение JWT сессии под секретом [session]: один читатель на процесс,
    поколение сессий выбирается здесь же и живёт до конца процесса."""
    session = config.session
    generation = session.session_generation()
    logger.info("session generation of this process: %s", generation)

    return JwtTokens(session.auth_secret, session.session_ttl_sec, generation)


def identity_users(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
) -> IdentityUsers:
    return IdentityUsers(config.postgres, config.db_schema, pool)


def sign_ins(
    config: Annotated[ProcessConfig, Depends(get_process_config)],
    directory: Annotated[UserDirectory, Depends(user_directory)],
    profiles: Annotated[ProfileCatalog, Depends(profile_catalog)],
    catalog: Annotated[IdentityUsers, Depends(identity_users)],
) -> SignIns:
    """Способы входа процесса из [auth]: пароли, SPNEGO и proxy с их
    провайдерами ролей и профилей."""
    services = AuthServices(directory, profiles, catalog, config.session.auth_secret)

    return config.AUTH_BACKENDS.build(config.auth, services)


def auth_service(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
    tokens: Annotated[JwtTokens, Depends(session_tokens)],
    entries: Annotated[SignIns, Depends(sign_ins)],
) -> AuthService:
    """Сессия приложения с браузером: способы входа, токен и cookie из
    [session], строки users."""
    session = config.session
    users = IdentityUsers(config.postgres, config.db_schema, pool)

    return AuthService(
        sign_ins=entries,
        tokens=tokens,
        cookie=session.cookie_spec(),
        users=users,
        renewal=session.renewal(),
    )


def command_runner(
    bus: Annotated[MessageBus, Depends(message_bus)],
    instance: Annotated[str, Depends(instance_name)],
    active: Annotated[Runs, Depends(runs)],
) -> CommandRunner:
    """Запускает исполнителя команд шины для запусков и ходов этого процесса."""
    runner = CommandRunner(bus, instance, active)
    runner.start()
    return runner


async def session_keeper(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    bus: Annotated[MessageBus, Depends(message_bus)],
    sessions: Annotated[LiveSessions, Depends(live_sessions)],
    tokens: Annotated[JwtTokens, Depends(session_tokens)],
) -> AsyncGenerator[SessionKeeper, None]:
    """Сторож сессий на всё время работы: сигнал обновления тем, чей токен на исходе."""
    keeper = SessionKeeper(
        bus, sessions, tokens, config.session.renewal(), config.cluster.heartbeat_sec
    )
    await keeper.start()
    try:
        yield keeper
    finally:
        await keeper.stop()
