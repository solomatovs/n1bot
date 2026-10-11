"""Провайдеры chainlit-процесса: конфиг чата, data layer, клиент MCP чата
(файлы, журналы, карточки инструментов), процессы агента ACP и приёмники
его потока. Клиенты LLM (llm_providers) остаются до вычистки langchain.

Общие для процессов провайдеры (реестр, сторы) — boba.runtime.providers.
"""

from collections.abc import AsyncIterator
from importlib.metadata import version
from typing import Annotated

import httpx
from omegaconf import DictConfig

from boba.auth import JwtTokens
from boba.chainlit.acp.chat import (
    AgentLauncher,
    AgentSessionMeta,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadDeleter,
)
from boba.chainlit.acp.config import AgentConfig
from boba.chainlit.acp.feed import AgentFeeds, BusParts, TabSurfaces
from boba.chainlit.acp.settings import (
    AgentSettingsLayers,
    AgentSettingsTable,
    ProfileModelSpecs,
)
from boba.chainlit.acp.tokens import McpAccessTokens, SessionServers
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.remote_storage import FileOwners, RemoteStorageClient
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.infra.config import (
    AppConfig,
    DataLayerConfig,
)
from boba.chainlit.infra.session import (
    ChainlitSessions,
    current_session,
    session_source_ref,
)
from boba.chainlit.rendering.acp_view import ToolMedia
from boba.chat.profiles import (
    AgentSettings,
    ChatProfiles,
    PrefetchFlowConfig,
    SelectedProfile,
    SettingsView,
    UserMeta,
)
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig
from boba.connections.secrets import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import UserRows
from boba.identity.context import CallContexts
from boba.identity.locks import LiveLocks
from boba.identity.session import SessionSource
from boba.llm.providers import LlmProviders, LlmProviderTypes
from boba.mcp_client.client import DroppedSignals, McpServers, NamedBlocks
from boba.messaging import MessageBus, PayloadStore
from boba.runtime import providers as runtime
from boba.runtime.config import RuntimeConfig
from boba.runtime.di import Container, Depends
from boba.runtime.elements import ChatTables
from boba.runtime.storage import StorageClient
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.toolrun.hosted import DirectCalls, HostedTool
from boba.toolrun.registry import ToolRegistry


def get_app_config() -> AppConfig:
    """Конфиг chainlit-процесса; значение кладёт bootstrap после AppConfig.load."""
    msg = (
        "get_app_config: the app config is set by bootstrap as a container "
        "override, the provider itself produces nothing"
    )
    raise RuntimeError(msg)


def get_data_layer_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> DataLayerConfig:
    return app_config.data_layer


def attachment_links(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> AttachmentLinks:
    """Ссылки на вложения под префиксом приложения."""
    return AttachmentLinks(app_config.chainlit.url_prefix)


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
    links: Annotated[AttachmentLinks, Depends(attachment_links)],
    owners: Annotated[FileOwners, Depends(file_owners)],
) -> StorageClient:
    """Хранилище вложений чата: файлы лежат в workspace пользователя на
    MCP-сервере его профиля."""
    return RemoteStorageClient(links.public_prefix(), owners, mcp_servers_ref)


def session_profile(
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
) -> SelectedProfile:
    """Профиль текущей сессии; без доступного профиля — отказ.

    Ошибки:
    RefusalError — профиль не выбран или недоступен ролям пользователя.
    """
    session = current_session()

    return registry.resolve(session.chat_profile, session.sign_in)


def session_settings_view(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> SettingsView:
    """Профиль сессии, поверх которого легли личные настройки пользователя."""
    meta = UserMeta.of(current_session().metadata)
    return SettingsView.of(
        app_config.settings,
        selected.config,
        meta.overrides_for(selected.name),
    )


def session_agent_settings(
    view: Annotated[SettingsView, Depends(session_settings_view, scope="session")],
) -> AgentSettings:
    """Настройки, с которыми идёт ход."""
    return view.agent()


def session_source(
    tokens: Annotated[JwtTokens, Depends(runtime.session_tokens)],
) -> SessionSource:
    """Источник сессий приложения; реализация знает про chainlit."""
    return ChainlitSessions(tokens)


def session_tools(
    registry: Annotated[ToolRegistry, Depends(runtime.tool_registry)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> list[HostedTool]:
    return registry.for_session(current_session().roles, selected.name)


async def llm_providers(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> AsyncIterator[LlmProviders]:
    """Модели процесса: бэкенды всех профилей собираются на старте, чтобы
    первая сессия не ждала весов локальной модели; закрываются на остановке."""
    providers = LlmProviders(LlmProviderTypes.installed())
    for profile in c.profiles.values():
        providers.chat(profile)

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            continue

        if flow.rephraser is None:
            continue

        providers.chat(flow.rephraser)

    try:
        yield providers
    finally:
        await providers.aclose()


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


async def chainlit_data_layer(  # noqa: PLR0913 — слой данных собирается всеми зависимостями сразу
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    links: Annotated[AttachmentLinks, Depends(attachment_links)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    sessions: Annotated[SessionSource, Depends(session_source)],
) -> PostgresDataLayer:
    """Слой данных чата на общем пуле процесса: схему таблицы ставят в запрос."""
    pool = await AsyncPostgresPool.get(cfg.postgres)
    tables = ChatTables.around(users, cfg.postgres, cfg.db_schema, pool)
    await tables.setup()

    return PostgresDataLayer(
        users=tables.users,
        threads=tables.threads,
        elements=tables.elements,
        feedbacks=tables.feedbacks,
        storage=storage,
        links=links,
        sessions=sessions,
        bus=bus,
    )


async def mcp_servers(
    c: Annotated[AppConfig, Depends(get_app_config)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
) -> AsyncIterator[McpServers]:
    """MCP-серверы процесса: подключаются на старте, закрываются на остановке.
    Инструменты через них зовёт агент, а не чат: чату нужны файлы серверов,
    журналы вызовов и карточки инструментов, поэтому блоки-файлы и отчёты о
    ходе вызовов приёмников не имеют, вызовы идут напрямую."""
    servers = McpServers(
        c.mcp, NamedBlocks(), DroppedSignals(), contexts, DirectCalls()
    )
    await servers.start()
    try:
        yield servers
    finally:
        await servers.stop()


def agent_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> AgentConfig:
    return app_config.agent


async def agent_settings_table(
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    raw: Annotated[DictConfig, Depends(runtime.get_raw_config)],
) -> AgentSettingsTable:
    """Слои настроек агента в схеме чата; секреты строк — под ключом
    [connections].encryption_key."""
    cipher = SecretCipher(bind(raw, "connections", ConnectionsConfig).key_bytes())
    table = AgentSettingsTable(cfg.postgres, cfg.db_schema, cipher)
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


async def mcp_access_tokens() -> AsyncIterator[McpAccessTokens]:
    """Клиент обмена утверждений на токены boba-mcp: один http-клиент на процесс."""
    http = httpx.AsyncClient(trust_env=False)
    try:
        yield McpAccessTokens(http)
    finally:
        await http.aclose()


def agent_launcher(
    config: Annotated[AgentConfig, Depends(agent_config)],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    layers: Annotated[AgentSettingsLayers, Depends(agent_settings_layers)],
    tokens: Annotated[McpAccessTokens, Depends(mcp_access_tokens)],
) -> AgentLauncher:
    """Запуск процессов агента: файл настроек, серверы MCP сессии, рукопожатие."""
    servers = SessionServers(app_config.mcp, tokens, config.chat_mcp, config.public_url)

    return AgentLauncher(config, layers, servers, version("boba-chainlit"))


def agent_session_meta(
    threads: Annotated[ThreadsTable, Depends(runtime.threads_table)],
) -> AgentSessionMeta:
    return AgentSessionMeta(threads)


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
