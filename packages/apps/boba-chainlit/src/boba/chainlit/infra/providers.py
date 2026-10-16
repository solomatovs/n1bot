"""Провайдеры chainlit-процесса: конфиг чата, data layer, клиент MCP чата
(файлы, журналы, карточки инструментов), процессы агента ACP и приёмники
его потока.

Общие для процессов провайдеры (реестр, сторы) — boba.runtime.providers.
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from importlib.metadata import version
from typing import Annotated

from omegaconf import DictConfig

from boba.auth import JwtTokens
from boba.chainlit.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    AgentSettingsChanges,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadDeleter,
)
from boba.chainlit.agent.config import AgentConfig
from boba.chainlit.agent.feed import AgentFeeds, BusParts, TabSurfaces
from boba.chainlit.agent.relay import McpRelay
from boba.chainlit.agent.server import (
    CallSealing,
    ChatCallers,
    ChatCallMiddleware,
    ChatMcpServer,
    ChatTokenVerifier,
    ChatToolServers,
    ProcessKeys,
)
from boba.chainlit.agent.settings import (
    AgentSettingsLayers,
    AgentSettingsTable,
    ProfileModelSpecs,
)
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.remote_storage import FileOwners, RemoteStorageClient
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.session import (
    ChainlitSessions,
    current_session,
    session_source_ref,
)
from boba.chainlit.rendering.acp_view import ToolMedia
from boba.chat.profiles import ChatProfiles, SelectedProfile
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import UserRows
from boba.identity.context import CallContexts
from boba.identity.locks import LiveLocks
from boba.identity.run import Runs
from boba.identity.session import SessionSource
from boba.mcp_client.client import McpServers
from boba.messaging import MessageBus, PayloadStore
from boba.runtime import providers as runtime
from boba.runtime.config import RuntimeConfig
from boba.runtime.di import Container, Depends
from boba.runtime.elements import ChatTables
from boba.runtime.storage import StorageClient
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.toolrun.registry import ToolRegistry


def get_app_config() -> AppConfig:
    """Конфиг chainlit-процесса; значение кладёт bootstrap после AppConfig.load."""
    msg = (
        "get_app_config: the app config is set by bootstrap as a container "
        "override, the provider itself produces nothing"
    )
    raise RuntimeError(msg)


def chat_profiles_registry(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> ChatProfiles:
    return ChatProfiles(app_config.profiles)


def mcp_servers_ref() -> McpServers:
    """MCP-серверы процесса из корневого контейнера; зовётся на каждую
    операцию с файлами."""
    return Container.require_root("chat providers").resolved(mcp_servers)


def remote_journals_ref() -> RemoteJournals:
    """Реестр журналов вызовов MCP-серверов из корневого контейнера."""
    return Container.require_root("chat providers").resolved(remote_journals)


def user_rows_ref() -> UserRows:
    """Строки users из корневого контейнера; зовётся на каждую операцию."""
    return Container.require_root("chat providers").resolved(runtime.users_table)


def file_owners(
    profiles: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
) -> FileOwners:
    """Владельцы файлов: по ключу объекта — пользователь и его сервер файлов."""
    return FileOwners(session_source_ref, user_rows_ref, profiles)


def storage_provider(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    owners: Annotated[FileOwners, Depends(file_owners)],
) -> StorageClient:
    """Хранилище вложений чата: файлы лежат в workspace пользователя на
    MCP-сервере его профиля."""
    return RemoteStorageClient(app_config.chainlit.url_prefix, owners, mcp_servers_ref)


def session_profile(
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
) -> SelectedProfile:
    """Профиль текущей сессии; без доступного профиля — отказ.

    Ошибки:
    RefusalError — профиль не выбран или недоступен ролям пользователя.
    """
    session = current_session()

    return registry.resolve(session.chat_profile, session.sign_in)


def session_source(
    tokens: Annotated[JwtTokens, Depends(runtime.session_tokens)],
) -> SessionSource:
    """Источник сессий приложения; реализация знает про chainlit."""
    return ChainlitSessions(tokens)


def thread_surfaces() -> TabSurfaces:
    """Поверхности вкладок для приёмников потока; значение кладёт bootstrap
    (реализация живёт в infra.thread_room, который сам читает провайдеры)."""
    msg = (
        "thread_surfaces: the surfaces are set by bootstrap as a container "
        "override, the provider itself produces nothing"
    )
    raise RuntimeError(msg)


def remote_journals() -> RemoteJournals:
    """Адреса журналов вызовов, исполненных MCP-серверами, на процесс:
    лента кладёт их по итогам вызовов агента."""
    return RemoteJournals()


def tool_media() -> ToolMedia:
    """Типы содержимого аргументов инструментов серверов MCP: по ним лента
    рисует вход шага вызова; наполняется приёмниками потока по профилю."""
    return ToolMedia()


async def chainlit_data_layer(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
) -> PostgresDataLayer:
    """Слой данных чата на пуле процесса: схему таблицы ставят в запрос."""
    tables = ChatTables.around(users, app_config.postgres, app_config.db_schema, pool)
    await tables.setup()

    return PostgresDataLayer(
        users=tables.users,
        threads=tables.threads,
        feedbacks=tables.feedbacks,
        storage=storage,
        bus=bus,
    )


async def mcp_servers(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> AsyncIterator[McpServers]:
    """MCP-серверы процесса: подключаются при первом обращении, закрываются
    на остановке. Через них идут вызовы агента (ретранслятор), файлы
    серверов, журналы вызовов и инструменты для ленты."""
    servers = McpServers(c.mcp)
    try:
        yield servers
    finally:
        await servers.stop()


def agent_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> AgentConfig:
    return app_config.agent


async def agent_settings_table(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    raw: Annotated[DictConfig, Depends(runtime.get_raw_config)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
) -> AgentSettingsTable:
    """Слои настроек агента в схеме чата; секреты строк — под ключом
    [connections].encryption_key."""
    cipher = SecretCipher(bind(raw, "connections", ConnectionsConfig).key_bytes())
    table = AgentSettingsTable(app_config.postgres, app_config.db_schema, cipher, pool)
    await table.setup()

    return table


def agent_settings_layers(
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
    config: Annotated[AgentConfig, Depends(agent_config)],
) -> AgentSettingsLayers:
    specs = ProfileModelSpecs(
        config.limits.context_window, config.limits.max_output_tokens
    )

    return AgentSettingsLayers(table, specs)


def process_keys() -> ProcessKeys:
    """Ключи процессов агента: один реестр на инстанс чата."""
    return ProcessKeys()


def chat_callers(
    keys: Annotated[ProcessKeys, Depends(process_keys)],
) -> ChatCallers:
    """Вызывающий по ключу процесса агента: для сервера инструментов чата и
    ретранслятора."""
    return ChatCallers(keys)


def call_sealing(
    raw: Annotated[DictConfig, Depends(runtime.get_raw_config)],
    callers: Annotated[ChatCallers, Depends(chat_callers)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
) -> CallSealing:
    """Запечатывание соединений пользователя ключом сервера MCP для
    ретранслятора mcp/message."""
    connections = ArmedConnections(
        runtime.connection_store_ref, runtime.credential_source_ref, contexts
    )
    ttl = timedelta(seconds=bind(raw, "connections", ConnectionsConfig).seal_ttl_sec)

    return CallSealing(callers, connections, contexts, ttl)


def mcp_relay(
    keys: Annotated[ProcessKeys, Depends(process_keys)],
    callers: Annotated[ChatCallers, Depends(chat_callers)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    sealing: Annotated[CallSealing, Depends(call_sealing)],
    config: Annotated[AgentConfig, Depends(agent_config)],
) -> McpRelay:
    """Ретранслятор mcp/message процессов агента: один на инстанс."""
    return McpRelay(keys, callers, servers, sealing, config.chat_mcp, config.public_url)


def agent_launcher(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    config: Annotated[AgentConfig, Depends(agent_config)],
    layers: Annotated[AgentSettingsLayers, Depends(agent_settings_layers)],
    keys: Annotated[ProcessKeys, Depends(process_keys)],
    relay: Annotated[McpRelay, Depends(mcp_relay)],
) -> AgentLauncher:
    """Запуск процессов агента: регистрации серверов профиля и чата, файл
    настроек, рукопожатие; mcp/message процессов отвечает ретранслятор."""
    return AgentLauncher(
        config, app_config.postgres, layers, keys, relay, version("boba-chainlit")
    )


def agent_session_meta(
    threads: Annotated[ThreadsTable, Depends(runtime.threads_table)],
) -> AgentSessionMeta:
    return AgentSessionMeta(threads)


def agent_settings_changes(
    layers: Annotated[AgentSettingsLayers, Depends(agent_settings_layers)],
) -> AgentSettingsChanges:
    """Единый путь правки настроек агента: слои и перезапуск процесса вкладки."""
    return AgentSettingsChanges(layers)


def thread_agents_index() -> ThreadAgentsIndex:
    """Агенты всех вкладок инстанса по тредам: один реестр на процесс."""
    return ThreadAgentsIndex()


def thread_deleter(
    launcher: Annotated[AgentLauncher, Depends(agent_launcher)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
) -> ThreadDeleter:
    return ThreadDeleter(launcher, table, index)


def agent_feeds(  # noqa: PLR0913 — фабрика собирается всеми входами процесса
    config: Annotated[RuntimeConfig, Depends(runtime.get_runtime_config)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    payloads: Annotated[PayloadStore, Depends(runtime.payload_store)],
    locks: Annotated[LiveLocks, Depends(runtime.live_locks)],
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    media: Annotated[ToolMedia, Depends(tool_media)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    data_layer: Annotated[PostgresDataLayer, Depends(chainlit_data_layer)],
    surfaces: Annotated[TabSurfaces, Depends(thread_surfaces)],
) -> AgentFeeds:
    """Приёмники потока агентов: ход в шину под замком треда, история — в
    вкладку, оценки шагов — из слоя данных."""
    parts = BusParts(
        bus=bus,
        payloads=payloads,
        locks=locks,
        heartbeat_sec=float(config.cluster.heartbeat_sec),
    )

    return AgentFeeds(parts, journals, media, servers, data_layer, surfaces)


async def thread_agents(
    launcher: Annotated[AgentLauncher, Depends(agent_launcher)],
    meta: Annotated[AgentSessionMeta, Depends(agent_session_meta)],
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
    feeds: Annotated[AgentFeeds, Depends(agent_feeds)],
) -> AsyncIterator[ThreadAgents]:
    """Агенты вкладки: процессы живут с DI-контейнером сессии chainlit и
    закрываются вместе с ним."""
    agents = ThreadAgents(launcher, meta, index, feeds)
    try:
        yield agents
    finally:
        await agents.close_all()


def chat_mcp_server(
    config: Annotated[AgentConfig, Depends(agent_config)],
    keys: Annotated[ProcessKeys, Depends(process_keys)],
    registry: Annotated[ToolRegistry, Depends(runtime.tool_registry)],
    callers: Annotated[ChatCallers, Depends(chat_callers)],
    runs: Annotated[Runs, Depends(runtime.runs)],
) -> ChatMcpServer:
    """MCP-сервер инструментов чата для агента под [agent.chat_mcp].path."""
    return ChatMcpServer(
        config.chat_mcp,
        ChatTokenVerifier(keys),
        ChatToolServers(registry, callers),
        ChatCallMiddleware(callers, runs),
    )
