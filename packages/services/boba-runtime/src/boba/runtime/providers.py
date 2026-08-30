"""Общие провайдеры процессов приложения: конфиг, сторы, реестр инструментов, входы.

Заглушки (get_runtime_config, plugin_table, instance_name) кладёт процесс через provide.

Ошибки:
RuntimeError — контейнер не поднят, секция выключена или процесс не дал значение.
"""

import logging
from collections.abc import AsyncGenerator, Sequence
from typing import Annotated

from omegaconf import DictConfig

from boba.access import GrantCheck
from boba.auth import AuthService, JwtTokens
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.auth.signin import PasswordSignIns
from boba.auth.sso import SpnegoGate, SsoSignIn
from boba.chat.profiles import RolesSection
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.db.pgvector.schema import KbSchema
from boba.identity.locks import RunLocking, StaleLock
from boba.identity.sso import RefreshSignal
from boba.identity.token import CookieSpec
from boba.messaging.bus import BusWatch
from boba.runtime.bus import PgMessageBus
from boba.runtime.commands import CommandRunner
from boba.runtime.config import AppName, RawConfig, RuntimeConfig
from boba.runtime.di import Container, Depends
from boba.runtime.journal import DirVault, StreamJournal
from boba.runtime.locks import LockReaper, PgLiveLocks
from boba.runtime.payloads import PgPayloadStore
from boba.runtime.plugins import PluginMeta, PluginTable, ToolLoader
from boba.runtime.refs import RuntimeRefs
from boba.runtime.threads import ThreadsTable
from boba.runtime.turns import StaleTurnCloser
from boba.runtime.users import UsersTable
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.streams import ToolStreams
from boba.workflow_engine.service import WorkflowService
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore

logger = logging.getLogger(__name__)


def get_raw_config() -> DictConfig:
    return RawConfig.get()


def get_runtime_config() -> RuntimeConfig:
    """Кладёт процесс после RuntimeConfig.load."""
    msg = "runtime config is provided by the process, not produced"
    raise RuntimeError(msg)


def app_name() -> AppName:
    """Какое приложение поднимает процесс; кладёт процесс через provide."""
    msg = "application name is provided by the process, not produced"
    raise RuntimeError(msg)


def instance_name(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    app: Annotated[AppName, Depends(app_name)],
) -> str:
    """Имя инстанса: узел из [cluster] плюс имя приложения."""
    return config.cluster.instance_of(app)


async def message_bus(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    app: Annotated[AppName, Depends(app_name)],
    instance: Annotated[str, Depends(instance_name)],
) -> AsyncGenerator[PgMessageBus, None]:
    """Поднимает шину процесса: готовит таблицы, запускает слушателя на всё время
    работы и останавливает его при закрытии контейнера.
    """
    bus = PgMessageBus(
        config.data_layer.postgres,
        config.cluster.db_schema,
        instance,
        app,
        config.cluster,
    )
    await bus.setup()
    await bus.start()

    try:
        yield bus
    finally:
        await bus.stop()


async def payload_store(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    bus: Annotated[PgMessageBus, Depends(message_bus)],
) -> PgPayloadStore:
    """Хранилище тел сообщений в live_payloads; схему готовит шина, поэтому она
    поднимается первой, таблицу — само хранилище.
    """
    store = PgPayloadStore(config.data_layer.postgres, config.cluster.db_schema)
    await store.setup()

    return store


def plugin_table() -> PluginTable:
    """Таблица плагинов процесса; кладёт процесс."""
    msg = "plugin table is provided by the process, not produced"
    raise RuntimeError(msg)


def refresh_signal() -> RefreshSignal:
    """Сигнал обновления билета входа; чат заменяет своим."""
    return NoRefresh()


def grant_check() -> GrantCheck:
    """Сверка грантов: процесс без chat-only инструментов проверяет только свои."""
    return GrantCheck.HOSTED


def _root() -> Container:
    root = Container.root
    if root is None:
        msg = "DI container is not initialised"
        raise RuntimeError(msg)

    return root


def credential_source(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    refresh: Annotated[RefreshSignal, Depends(refresh_signal)],
) -> KerberosCredentialSource:
    """Источник кредов вызова на процесс; без kerberos в [auth] делегирование отказывает."""
    return KerberosCredentialSource(config.sso_tickets(), refresh)


def credential_source_ref() -> KerberosCredentialSource:
    """Источник кредов вызова из корневого контейнера; зовётся на каждый вызов."""
    return _root().resolved(credential_source)


def bus_watch_ref() -> BusWatch:
    """Возвращает слушателя шины процесса, по которому страница показывает состояние
    живой связи.
    """
    return _root().resolved(message_bus).listener


def message_bus_ref() -> PgMessageBus:
    """Шина процесса для обвязок инструментов; зовётся на каждый вызов."""
    return _root().resolved(message_bus)


def connection_store_ref() -> ConnectionStore:
    """Хранилище соединений для обвязок инструментов; зовётся на каждый вызов."""
    store = _root().resolved(connection_store)
    if store is None:
        msg = "[connections] is disabled: user connections are unavailable"
        raise RuntimeError(msg)

    return store


def runtime_refs() -> RuntimeRefs:
    """Входы приложения для api и обвязок: ссылки в корневой контейнер."""
    return RuntimeRefs(
        tool_registry=tool_registry_ref,
        workflow_service=workflow_service_ref,
        connection_store=connection_store_ref,
        credentials=credential_source_ref,
        live_locks=live_locks_ref,
        heartbeat_sec=_root().resolved(get_runtime_config).cluster.heartbeat_sec,
        bus_watch=bus_watch_ref,
        message_bus=message_bus_ref,
    )


def tool_registry(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
    table: Annotated[PluginTable, Depends(plugin_table)],
    check: Annotated[GrantCheck, Depends(grant_check)],
) -> ToolRegistry:
    refs = runtime_refs()
    loader = ToolLoader(raw, table(refs), refs, check)

    return loader.load()


async def tool_registry_ref() -> ToolRegistry:
    """Реестр инструментов из корневого контейнера: для вызовов вне сессии."""
    return await _root().resolve(Depends(tool_registry))


async def workflow_service_ref() -> WorkflowService:
    """Сервис workflow из корневого контейнера; зовётся на каждый вызов."""
    service = await _root().resolve(Depends(workflow_service))
    if service is None:
        msg = "[workflow] is disabled: workflows are unavailable"
        raise RuntimeError(msg)

    return service


async def kb_schema(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> None:
    """Готовит таблицы базы знаний, если секция [tool.kb] включена."""
    meta = bind(raw, "tool.kb", PluginMeta)
    if not meta.enable:
        return

    cfg = bind(raw, "tool.kb", PostgresKnowledgeBaseConfig)
    await KbSchema(cfg, dim=cfg.embedding.dim).setup()


async def workflow_store(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> WorkflowStore | None:
    """Хранилище workflow и их запусков; None — секция [workflow] выключена."""
    cfg = bind(raw, "workflow", WorkflowConfig)
    if not cfg.enable:
        return None

    store = WorkflowStore(cfg)
    await store.setup()

    return store


async def live_locks(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    instance: Annotated[str, Depends(instance_name)],
    app: Annotated[AppName, Depends(app_name)],
    bus: Annotated[PgMessageBus, Depends(message_bus)],
) -> PgLiveLocks:
    """Блокировки областей процесса; live_instances готовит шина, поэтому она
    поднимается первой, live_locks создаёт хранилище само, инстанс регистрируется
    здесь и дальше подтверждается сторожем.
    """
    locks = PgLiveLocks(
        config.data_layer.postgres,
        config.cluster.db_schema,
        instance,
        app,
        config.cluster,
    )
    await locks.setup()
    await locks.register_instance()
    return locks


def live_locks_ref() -> PgLiveLocks:
    """Блокировки для обвязок инструментов; зовётся на каждый вызов."""
    return _root().resolved(live_locks)


def workflow_service(
    store: Annotated[WorkflowStore | None, Depends(workflow_store)],
    instance: Annotated[str, Depends(instance_name)],
    bus: Annotated[PgMessageBus, Depends(message_bus)],
    locks: Annotated[PgLiveLocks, Depends(live_locks)],
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> WorkflowService | None:
    """Сервис workflow; события запусков уходят в шину процесса под блокировкой."""
    if store is None:
        return None

    locking = RunLocking(locks=locks, heartbeat_sec=config.cluster.heartbeat_sec)
    return WorkflowService(store, tool_registry_ref, instance, bus, locking)


async def workflow_recovery(
    service: Annotated[WorkflowService | None, Depends(workflow_service)],
) -> None:
    """Запуски этого инстанса без процесса закрываются на старте, а не висят running."""
    if service is None:
        return

    recovered = await service.recover_orphans()
    if recovered:
        logger.warning("workflow: %d abandoned run(s) closed on startup", recovered)


async def connection_store(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> ConnectionStore | None:
    """Хранилище соединений; роли из [roles] попадают в таблицу roles на старте."""
    cfg = bind(raw, "connections", ConnectionsConfig)
    if not cfg.enable:
        return None

    store = ConnectionStore(cfg)
    await store.setup()

    roles = bind(raw, "roles", RolesSection).root
    await store.sync_roles(roles)

    return store


def stream_journal(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> None:
    """Журнал живого вывода инструментов на процесс; без секции потоков нет."""
    section = config.stream_journal
    if not section.enable:
        return

    vault = DirVault(section.dir)
    ToolStreams.configure(StreamJournal(vault, section.reserve_bytes))


def users_table(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> UsersTable:
    """Строки users и авторы тредов той же схемы, что у data layer чата."""
    return UsersTable(config.data_layer.postgres, config.data_layer.db_schema)


def threads_table(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> ThreadsTable:
    """Строки threads той же схемы, что у data layer чата."""
    return ThreadsTable(config.data_layer.postgres, config.data_layer.db_schema)


def auth_service(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    table: Annotated[UsersTable, Depends(users_table)],
) -> AuthService:
    """Вход пользователя: пароли и SPNEGO из [auth], токен и cookie из [session]."""
    session = config.session
    tokens = JwtTokens(session.auth_secret, session.session_ttl_sec)
    cookie = CookieSpec(
        name=session.cookie,
        samesite=session.cookie_samesite,
        ttl_sec=session.session_ttl_sec,
    )

    sso = None
    if kerberos := config.kerberos():
        sso = SpnegoGate(SsoSignIn(kerberos, session.auth_secret))

    return AuthService(
        tokens=tokens,
        cookie=cookie,
        password=PasswordSignIns.of(config.auth),
        sso=sso,
        users=table,
    )


async def lock_reaper(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
    locks: Annotated[PgLiveLocks, Depends(live_locks)],
    service: Annotated[WorkflowService | None, Depends(workflow_service)],
    bus: Annotated[PgMessageBus, Depends(message_bus)],
    payloads: Annotated[PgPayloadStore, Depends(payload_store)],
) -> AsyncGenerator[LockReaper, None]:
    """Запускает сторожа блокировок на всё время работы: он закрывает ходы и запуски
    без держателя, следит за очередью уведомлений и убирает старые события; при
    остановке снимает блокировки инстанса.
    """

    async def on_stale(stale: Sequence[StaleLock]) -> None:
        for lock in stale:
            logger.warning(
                "stale lock removed: %s held by %s for %s",
                lock.scope.render(),
                lock.holder,
                lock.purpose.value,
            )

        turns = await StaleTurnCloser(bus, locks).close(stale)
        if turns:
            logger.warning("chat: %d turn(s) of dead holders closed", turns)

        if service is None:
            return

        closed = await service.close_unlocked()
        if closed:
            logger.warning("workflow: %d run(s) without a holder closed", closed)

    async def on_sweep() -> None:
        usage = await bus.queue_usage()
        if usage > config.cluster.queue_usage_limit:
            # долю очереди держит соединение слушателя: освобождает только разрыв
            logger.error(
                "notification queue usage %.3f exceeds %.3f: reconnecting the listener",
                usage,
                config.cluster.queue_usage_limit,
            )
            await bus.listener.reconnect()

        removed = await bus.purge_idle(config.cluster.retention_sec)
        bodies = await payloads.purge_idle(config.cluster.retention_sec)
        if removed or bodies:
            logger.info("live retention: %d events, %d bodies removed", removed, bodies)

    reaper = LockReaper(locks, config.cluster.reaper_period_sec, on_stale, on_sweep)
    await reaper.start()
    try:
        yield reaper
    finally:
        await reaper.stop()
        await locks.release_all(locks.instance)


def command_runner(
    bus: Annotated[PgMessageBus, Depends(message_bus)],
    instance: Annotated[str, Depends(instance_name)],
) -> CommandRunner:
    """Запускает исполнителя команд шины для запусков и ходов этого процесса."""
    runner = CommandRunner(bus, instance)
    runner.start()
    return runner
