"""Общие провайдеры процессов приложения: конфиг, сторы, реестр инструментов, входы.

Заглушки (get_runtime_config, plugin_table, instance_name) кладёт процесс через provide.

Ошибки:
RuntimeError — контейнер не поднят, секция выключена или процесс не дал значение.
"""

import socket
from typing import Annotated

from omegaconf import DictConfig

from boba.access import GrantCheck
from boba.chat.profiles import RolesSection
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import RefreshSignal
from boba.db.pgvector.schema import KbSchema
from boba.krb.seal import SsoTickets
from boba.runtime.config import RawConfig, RuntimeConfig
from boba.runtime.di import Container, Depends
from boba.runtime.plugins import PluginMeta, PluginTable, ToolLoader
from boba.runtime.refs import RuntimeRefs
from boba.runtime.users import UsersTable
from boba.settings import bind
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.toolrun.registry import ToolRegistry
from boba.workflow.events import RunEvents
from boba.workflow_engine.service import WorkflowService
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore


class NoRefresh(RefreshSignal):
    """Процесс без сокета сессии: просить браузер обновить билет некому."""

    async def send(self) -> bool:
        return False


def get_raw_config() -> DictConfig:
    return RawConfig.get()


def get_runtime_config() -> RuntimeConfig:
    """Кладёт процесс после RuntimeConfig.load."""
    msg = "runtime config is provided by the process, not produced"
    raise RuntimeError(msg)


def instance_name() -> str:
    """Имя инстанса для запусков workflow (host:port); кладёт процесс."""
    return f"{socket.gethostname()}:{_root().resolved(get_runtime_config).studio.port}"


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


def sso_tickets_ref() -> SsoTickets | None:
    """Открыватель билетов SSO-входа; None — kerberos в [auth] не настроен."""
    return _root().resolved(get_runtime_config).sso_tickets()


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
        sso_tickets=sso_tickets_ref,
    )


def tool_registry(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
    table: Annotated[PluginTable, Depends(plugin_table)],
    refresh: Annotated[RefreshSignal, Depends(refresh_signal)],
    check: Annotated[GrantCheck, Depends(grant_check)],
) -> ToolRegistry:
    refs = runtime_refs()
    loader = ToolLoader(raw, table(refs), refs, refresh, check)

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


def workflow_service(
    store: Annotated[WorkflowStore | None, Depends(workflow_store)],
    instance: Annotated[str, Depends(instance_name)],
) -> WorkflowService | None:
    """Сервис workflow; инстанс — host:port, чтобы различать запуски реплик."""
    if store is None:
        return None

    return WorkflowService(store, tool_registry_ref, instance, RunEvents())


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


def users_table(
    config: Annotated[RuntimeConfig, Depends(get_runtime_config)],
) -> UsersTable:
    """Строки users и авторы тредов той же схемы, что у data layer чата."""
    return UsersTable(config.data_layer.postgres, config.data_layer.db_schema)
