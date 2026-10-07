"""Провайдеры chainlit-процесса: конфиг чата, клиенты LLM, data layer и агент сессии.

Общие для процессов провайдеры (реестр, сторы) — boba.runtime.providers.
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from omegaconf import DictConfig

from boba.auth import JwtTokens
from boba.chainlit.agent.bridge import ChatModelBridge
from boba.chainlit.agent.flow import (
    AgentGraphBuilder,
    GraphSpec,
    HistoryView,
    LlmRephraser,
    PassthroughRephraser,
    PlainGraphBuilder,
    PrefetchGraphBuilder,
    Rephraser,
)
from boba.chainlit.agent.history import (
    CheckpointMessages,
    GraphAgent,
    PostgresCheckpoints,
)
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.chat.dialog import SessionAgent
from boba.chainlit.chat.history import TranscriptFeed
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.remote_storage import FileOwners, RemoteStorageClient
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.infra.config import (
    AppConfig,
    CheckpointerConfig,
    DataLayerConfig,
)
from boba.chainlit.infra.session import (
    ChainlitSessions,
    current_session,
    session_source_ref,
)
from boba.chainlit.rendering.mount import (
    ChatAttachments,
    ChatCalls,
    ChatMount,
    ChatProgress,
)
from boba.chat.profiles import (
    AgentSettings,
    ChatProfiles,
    PrefetchFlowConfig,
    SelectedProfile,
    SettingsView,
    UserMeta,
)
from boba.config import bind
from boba.connection_broker.sealing import SealingToolServer, SentConnections
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.user_connections import ArmedConnections
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema
from boba.identity.api import UserRows
from boba.identity.context import CallContexts
from boba.identity.errors import InternalServiceError
from boba.identity.run import Runs
from boba.identity.session import SessionSource
from boba.llm.providers import LlmProviders, LlmProviderTypes
from boba.llm.schema import SchemaReply
from boba.mcp_client.client import McpCaller, McpServers
from boba.messaging import MessageBus
from boba.runtime import providers as runtime
from boba.runtime.di import Container, Depends
from boba.runtime.elements import ChatTables
from boba.runtime.storage import StorageClient
from boba.runtime.users import UsersTable
from boba.toolkit.dag import NodeCalls, ToolServer
from boba.toolrun.hosted import HostedTool
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.stream_calls import ToolServers


def get_app_config() -> AppConfig:
    """Конфиг chainlit-процесса; значение кладёт bootstrap после AppConfig.load."""
    msg = (
        "get_app_config: the app config is set by bootstrap as a container "
        "override, the provider itself produces nothing"
    )
    raise RuntimeError(msg)


def get_checkpointer_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> CheckpointerConfig:
    return app_config.checkpointer


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


async def langchain_checkpoint_saver(
    cp: Annotated[CheckpointerConfig, Depends(get_checkpointer_config)],
) -> CheckpointMessages:
    """История тредов процесса: checkpoint'ы langgraph на пуле со своим
    search_path — схему в имена савер не ставит.

    Пул закрывает остановка приложения, а не провайдер: пул общий для всех, кто
    попросит его с той же схемой.
    """
    pool = await AsyncPostgresPool.get(cp.postgres.with_schema(cp.db_schema))
    try:
        await PostgresSchema(cp.db_schema).ensure_with(pool)
    except PostgresError as e:
        raise InternalServiceError(
            internal_detail=(
                f"checkpointer: ensuring postgres schema {cp.db_schema!r} failed: {e}"
            ),
            user_detail="Failed to connect to the internal postgres",
        ) from e

    checkpoints = PostgresCheckpoints(pool)
    await checkpoints.setup()

    return checkpoints


def remote_journals(
    saver: Annotated[CheckpointMessages, Depends(langchain_checkpoint_saver)],
) -> RemoteJournals:
    """Адреса журналов вызовов, исполненных MCP-серверами, на процесс."""
    return RemoteJournals(saver)


async def chainlit_data_layer(  # noqa: PLR0913 — слой данных собирается всеми зависимостями сразу
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    links: Annotated[AttachmentLinks, Depends(attachment_links)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    saver: Annotated[CheckpointMessages, Depends(langchain_checkpoint_saver)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    sessions: Annotated[SessionSource, Depends(session_source)],
    streamable: Annotated[RemoteJournals, Depends(remote_journals)],
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
        feed=TranscriptFeed(saver, streamable),
        links=links,
        sessions=sessions,
        bus=bus,
    )


def session_graph_builder(
    providers: Annotated[LlmProviders, Depends(llm_providers)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> AgentGraphBuilder:
    """Билдер графа хода по flow профиля."""
    flow = selected.config.flow
    if not isinstance(flow, PrefetchFlowConfig):
        return PlainGraphBuilder()

    return PrefetchGraphBuilder(_rephraser(providers, flow), flow.tools)


def _rephraser(providers: LlmProviders, flow: PrefetchFlowConfig) -> Rephraser:
    """Модель, готовящая поисковые запросы; без секции — запрос идёт как есть."""
    cfg = flow.rephraser
    if cfg is None:
        return PassthroughRephraser()

    reply = SchemaReply(providers.chat(cfg), cfg.sampling)

    return LlmRephraser(reply, cfg.system_prompt)


def session_chat(
    providers: Annotated[LlmProviders, Depends(llm_providers)],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
) -> ChatModelBridge:
    """Чат-модель хода: мост графа поверх модели профиля с сэмплингом сессии."""
    return ChatModelBridge(
        chat_model=providers.chat(settings),
        sampling=dict(settings.sampling),
        model_name=settings.model,
    )


def sent_connections() -> SentConnections:
    """Что чат отправил серверу инструментов вместо ссылок на соединения:
    один объект на процесс, общий для порта инструментов и ленты."""
    return SentConnections()


def chat_mount(
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
    runs: Annotated[Runs, Depends(runtime.runs)],
) -> ChatMount:
    """Монтирование результатов вызовов на поверхность чата: один объект на
    процесс, общий для пути вызова узла и вложений MCP-серверов."""
    return ChatMount(contexts, runs)


def node_calls(
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
    runs: Annotated[Runs, Depends(runtime.runs)],
    sent: Annotated[SentConnections, Depends(sent_connections)],
    mount: Annotated[ChatMount, Depends(chat_mount)],
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
) -> ChatCalls:
    """Путь вызова узла к телу в чате: вызов виден ленте идущего хода шагом.
    Один объект на процесс: его берут исполнитель своих инструментов и
    порты MCP-серверов."""
    return ChatCalls(contexts, runs, sent, mount, journals)


async def mcp_servers(  # noqa: PLR0913 — клиент собирается всеми входами чата
    c: Annotated[AppConfig, Depends(get_app_config)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
    mount: Annotated[ChatMount, Depends(chat_mount)],
    runs: Annotated[Runs, Depends(runtime.runs)],
    calls: Annotated[NodeCalls, Depends(node_calls)],
) -> AsyncIterator[McpServers]:
    """MCP-серверы процесса: подключаются на старте, закрываются на остановке.
    Файлы из их результатов ложатся в workspace треда вложениями чата,
    отчёты серверов о ходе работы вызовов уходят шагам ленты."""
    files = ChatAttachments(contexts, storage, mount)
    servers = McpServers(c.mcp, files, ChatProgress(contexts, runs), contexts, calls)
    await servers.start()
    try:
        yield servers
    finally:
        await servers.stop()


async def langchain_agent(  # noqa: PLR0913
    chat: Annotated[ChatModelBridge, Depends(session_chat, scope="session")],
    builder: Annotated[
        AgentGraphBuilder, Depends(session_graph_builder, scope="session")
    ],
    saver: Annotated[CheckpointMessages, Depends(langchain_checkpoint_saver)],
    tools: Annotated[list[HostedTool], Depends(session_tools, scope="session")],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
    registry: Annotated[ToolRegistry, Depends(runtime.tool_registry)],
    raw: Annotated[DictConfig, Depends(runtime.get_raw_config)],
    sent: Annotated[SentConnections, Depends(sent_connections)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
    mcp: Annotated[McpServers, Depends(mcp_servers)],
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    calls: Annotated[ChatCalls, Depends(node_calls)],
) -> SessionAgent:
    """Агент сессии: граф хода по flow профиля над портом инструментов."""
    # один порт для графа: свои серверы и MCP-серверы профиля сессии.
    # На MCP-сервер чат входит от имени пользователя: его identifier — логин,
    # роли — те, с которыми он вошёл в чат. Соединения запечатываются на
    # каждом сервере его собственным ключом
    caller = McpCaller(
        login=current_session().identifier, roles=current_session().roles
    )
    remote = await mcp.for_session(caller, selected.config.mcp)
    journals.mark_streamable(mcp.journaled())
    connections = ArmedConnections(
        runtime.connection_store_ref, runtime.credential_source_ref, contexts
    )
    ttl = timedelta(seconds=bind(raw, "connections", ConnectionsConfig).seal_ttl_sec)

    ports: list[ToolServer] = [
        SealingToolServer(registry.server(tools, calls), connections, sent, ttl, calls)
    ]
    for port in remote:
        ports.append(SealingToolServer(port, connections, sent, ttl, calls))
        calls.shown(port.tools())

    service = ToolServers(ports)

    spec = GraphSpec(
        chat=chat,
        service=service,
        system_prompt=settings.system_prompt,
        checkpoints=saver,
        history=HistoryView(service.names(), settings.history_messages),
    )

    return GraphAgent(builder.build(spec))
