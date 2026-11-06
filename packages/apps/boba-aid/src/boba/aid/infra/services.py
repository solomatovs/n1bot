"""Сборка хранилищ, входа и обмена сообщениями AID.

Ошибки:
RuntimeError — корень AID не предоставил конфигурацию или зависимость.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from typing import Annotated

from omegaconf import DictConfig

from boba.aid.auth.session import BrowserSessions
from boba.aid.auth.tokens import JwtTokens
from boba.aid.config.app import AppConfig
from boba.aid.data.threads import ThreadsTable
from boba.aid.data.users import UsersTable
from boba.aid.infra.commands import CommandRunner
from boba.aid.infra.di import Container, Depends
from boba.aid.infra.messaging import (
    MessagingRuntime,
    MessagingServices,
    MessagingSettings,
    StaleObserverFactory,
)
from boba.aid.infra.refresh import LiveSessions, SessionKeeper
from boba.auth import SessionService, SignIns
from boba.auth.assembly import AuthServices
from boba.chat.profiles import ChatProfiles
from boba.core.components import Components
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContexts
from boba.identity.directory import UserDirectory
from boba.identity.locks import LiveLocks
from boba.identity.run import Runs
from boba.identity.signin import ProfileCatalog
from boba.identity_postgres import UsersTable as IdentityUsers
from boba.ldap import Ldap3Directory
from boba.messaging import (
    MessageBus,
    MessageCodec,
    PayloadStore,
)
from boba.messaging.config import AppName, MessagingConfig
from boba.tool_io.chain import CallAmbient

logger = logging.getLogger(__name__)


def get_raw_config() -> DictConfig:
    raise RuntimeError("AID config tree must be supplied by the application")


def application_config() -> AppConfig:
    """Конфиг AID, переданный bootstrap после чтения файлов настроек."""
    msg = (
        "DI provider application_config resolved before the process supplied "
        "AppConfig via Container.provide"
    )
    raise RuntimeError(msg)


def app_name() -> AppName:
    """Какое приложение поднимает процесс; кладёт процесс через provide."""
    msg = (
        "DI provider app_name resolved before the process supplied AppName "
        "via Container.provide"
    )
    raise RuntimeError(msg)


def instance_name(
    config: Annotated[AppConfig, Depends(application_config)],
    app: Annotated[AppName, Depends(app_name)],
) -> str:
    """Имя инстанса: узел из [cluster] плюс имя приложения."""
    return config.cluster.instance_of(app)


async def postgres_pool(
    config: Annotated[AppConfig, Depends(application_config)],
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
    config: Annotated[AppConfig, Depends(application_config)],
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
    settings = MessagingSettings(
        postgres=config.postgres,
        db_schema=config.db_schema,
        cluster=config.cluster,
    )
    services = MessagingServices(settings, instance, app, pool, codec, observer)
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


def _root() -> Container:
    return Container.require_root("AID services")


def runs_ref() -> Runs:
    """Реестр идущих запусков из корневого контейнера."""
    return _root().resolved(runs)


def profile_catalog(
    config: Annotated[AppConfig, Depends(application_config)],
) -> ProfileCatalog:
    """Каталог профилей чата AID из секции [profiles]."""
    return ChatProfiles(config.profiles)


def live_locks(
    runtime: Annotated[MessagingRuntime, Depends(messaging_runtime)],
) -> LiveLocks:
    return runtime.locks


def call_ambient() -> CallAmbient:
    """Обстановка вызова инструмента процесса: одна на всех, кто её ставит
    и читает."""
    return CallAmbient()


def runs(
    contexts: Annotated[CallContexts, Depends(call_contexts)],
) -> Runs:
    """Реестр идущих запусков процесса."""
    return Runs(contexts)


async def users_table(
    config: Annotated[AppConfig, Depends(application_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
) -> UsersTable:
    """Строки users и авторы тредов в схеме приложения на пуле процесса."""
    table = UsersTable(pool, config.db_schema)
    return table


def threads_table(
    config: Annotated[AppConfig, Depends(application_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
) -> ThreadsTable:
    """Строки threads в схеме приложения на пуле процесса."""
    return ThreadsTable(pool, config.db_schema)


def session_tokens(
    config: Annotated[AppConfig, Depends(application_config)],
) -> JwtTokens:
    """Выпуск и чтение JWT сессии под секретом [session]: один читатель на процесс,
    поколение сессий выбирается здесь же и живёт до конца процесса."""
    session = config.session
    generation = session.session_generation()
    logger.info("session generation of this process: %s", generation)

    return JwtTokens(
        secret=session.auth_secret,
        lifetime=session.lifetime(),
        generation=generation,
        issuer=config.server.public_url,
        audience=config.server.public_url,
    )


def identity_users(
    config: Annotated[AppConfig, Depends(application_config)],
    pool: Annotated[AsyncPostgresPool, Depends(postgres_pool)],
) -> IdentityUsers:
    return IdentityUsers(pool, config.db_schema)


def sign_ins(
    config: Annotated[AppConfig, Depends(application_config)],
    directory: Annotated[UserDirectory, Depends(user_directory)],
    profiles: Annotated[ProfileCatalog, Depends(profile_catalog)],
    catalog: Annotated[IdentityUsers, Depends(identity_users)],
) -> SignIns:
    """Способы входа процесса из [auth]: пароли, SPNEGO и proxy с их
    провайдерами ролей и профилей."""
    services = AuthServices(directory, profiles, catalog, config.session.auth_secret)

    return config.AUTH_BACKENDS.build(config.auth, services)


def authenticated_sessions(
    users: Annotated[IdentityUsers, Depends(identity_users)],
    profiles: Annotated[ProfileCatalog, Depends(profile_catalog)],
) -> SessionService:
    return SessionService(users, profiles)


def auth_service(
    config: Annotated[AppConfig, Depends(application_config)],
    tokens: Annotated[JwtTokens, Depends(session_tokens)],
    entries: Annotated[SignIns, Depends(sign_ins)],
    sessions: Annotated[SessionService, Depends(authenticated_sessions)],
) -> BrowserSessions:
    """Браузерный адаптер над общими сценариями сессии."""
    session = config.session
    return BrowserSessions(
        sign_ins=entries,
        tokens=tokens,
        cookie=session.cookie_spec(),
        sessions=sessions,
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
    config: Annotated[AppConfig, Depends(application_config)],
    bus: Annotated[MessageBus, Depends(message_bus)],
    sessions: Annotated[LiveSessions, Depends(live_sessions)],
    tokens: Annotated[JwtTokens, Depends(session_tokens)],
) -> AsyncGenerator[SessionKeeper, None]:
    """Сторож сессий на всё время работы: сигнал обновления тем, чей токен на исходе."""
    keeper = SessionKeeper(
        bus,
        sessions,
        tokens,
        config.session.renewal(),
        config.cluster.heartbeat_sec,
    )
    await keeper.start()
    try:
        yield keeper
    finally:
        await keeper.stop()
