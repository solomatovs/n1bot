"""Провайдеры процесса aid: конфиг чата, data layer, клиент MCP чата
(файлы, журналы, карточки инструментов), процессы агента ACP и приёмники
его потока.

Общие для процессов провайдеры (реестр, сторы) — boba.runtime.providers.
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from enum import StrEnum
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from chainlit.config import config as chainlit_config

from boba.aid.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadDeleter,
)
from boba.aid.agent.config import AgentConfig
from boba.aid.agent.feed import AgentFeeds, BusParts
from boba.aid.agent.relay import ChatTools, McpRegistrations, McpRelay
from boba.aid.canvas.remote import RemoteJournals
from boba.aid.canvas.tools import CanvasOpener
from boba.aid.chat.ui_text import UiText
from boba.aid.data import PostgresDataLayer
from boba.aid.data.remote_storage import FileOwners, RemoteStorageClient
from boba.aid.infra.config import AppConfig
from boba.aid.infra.session import (
    ChainlitSessions,
    current_session,
    session_source_ref,
)
from boba.aid.rendering.renderer import TabSurfaces
from boba.auth import JwtTokens
from boba.chat.profiles import ChatProfiles, SelectedProfile
from boba.connection_broker.sealing import CallSealing
from boba.connection_broker.user_connections import ArmedConnections
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
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
from boba.runtime.elements import FeedbacksTable
from boba.runtime.storage import StorageClient
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.toolrun.registry import ToolRegistry


def get_app_config() -> AppConfig:
    """Конфиг процесса aid; значение кладёт bootstrap после AppConfig.load."""
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
    return RemoteStorageClient(app_config.aid.url_prefix, owners, mcp_servers_ref)


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


def feed_text(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> UiText:
    """Подписи ленты: лента треда одна на все его вкладки, поэтому её язык —
    язык интерфейса развёртывания ([UI].language chainlit), без него — язык
    по умолчанию."""
    language = ""
    if forced := chainlit_config.ui.language:
        language = forced

    return UiText(Path(app_config.aid.root), language)


def remote_journals() -> RemoteJournals:
    """Адреса журналов вызовов, исполненных MCP-серверами, на процесс:
    лента кладёт их по итогам вызовов агента."""
    return RemoteJournals()


class HarnessSchema(StrEnum):
    """Пакеты, чьи файлы schema/*.sql чат применяет в схеме приложения на
    старте: таблицы чата и шины, соединения и гранты."""

    RUNTIME = "boba.runtime"
    CONNECTIONS = "boba.connection_broker"


async def harness_storage(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
) -> None:
    """Таблицы чата одним механизмом на старте процесса: файлы схемы каждого
    пакета HarnessSchema в схеме [app].schema. Поднимается первым после
    пула: таблицы процесса работают на готовой схеме."""
    schema = PostgresSchema(app_config.db_schema)
    async with pool.connection() as conn:
        for member in HarnessSchema:
            schema_dir = files(member.value) / "schema"
            await schema.apply_files(conn, schema_dir, {})


async def chainlit_data_layer(  # noqa: PLR0913 — слой данных собирается таблицами процесса
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    threads: Annotated[ThreadsTable, Depends(runtime.threads_table)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
    storage_ready: Annotated[None, Depends(harness_storage)],
) -> PostgresDataLayer:
    """Слой данных чата на пуле процесса: схему таблицы ставят в запрос."""
    feedbacks = FeedbacksTable(app_config.postgres, app_config.db_schema, pool)

    return PostgresDataLayer(
        users=users,
        threads=threads,
        feedbacks=feedbacks,
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


def mcp_registrations() -> McpRegistrations:
    """Регистрации серверов процессов агента: один реестр на инстанс чата."""
    return McpRegistrations()


def call_sealing(
    config: Annotated[AgentConfig, Depends(agent_config)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
) -> CallSealing:
    """Запечатывание соединений пользователя ключом сервера MCP для
    ретранслятора mcp/message."""
    connections = ArmedConnections(
        runtime.connection_store_ref, runtime.credential_source_ref, contexts
    )
    ttl = timedelta(seconds=config.seal_ttl_sec)

    return CallSealing(connections, ttl)


def canvas_opener(
    surfaces: Annotated[TabSurfaces, Depends(thread_surfaces)],
) -> CanvasOpener:
    """Показ файлов в панели канваса: по клику и по ответу tools/call."""
    return CanvasOpener(surfaces)


def mcp_relay(  # noqa: PLR0913 — ретранслятор собирается всеми входами процесса
    registrations: Annotated[McpRegistrations, Depends(mcp_registrations)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    sealing: Annotated[CallSealing, Depends(call_sealing)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
    registry: Annotated[ToolRegistry, Depends(runtime.tool_registry)],
    runs: Annotated[Runs, Depends(runtime.runs)],
    canvas: Annotated[CanvasOpener, Depends(canvas_opener)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
) -> McpRelay:
    """Ретранслятор mcp/message процессов агента: один на инстанс; сервер
    chat — собственные инструменты чата из реестра; файлы ответа, которые
    сервер просит показать, открывает панель канваса, журналы идущих вызовов
    называет ленте шина."""
    own = version("boba-aid")
    chat = ChatTools(registry, runs, own)

    return McpRelay(registrations, servers, sealing, contexts, chat, canvas, bus, own)


def agent_launcher(  # noqa: PLR0913 — запуск собирается всеми входами процесса
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    config: Annotated[AgentConfig, Depends(agent_config)],
    profiles: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    registrations: Annotated[McpRegistrations, Depends(mcp_registrations)],
    relay: Annotated[McpRelay, Depends(mcp_relay)],
) -> AgentLauncher:
    """Запуск процессов агента: регистрации серверов профиля и чата, файл
    настроек по текущему профилю, рукопожатие; mcp/message процессов отвечает
    ретранслятор. Профили конфига и строки users нужны удалению сессии треда."""
    return AgentLauncher(
        config,
        app_config.mcp,
        profiles,
        users,
        registrations,
        relay,
        version("boba-aid"),
    )


def agent_session_meta(
    threads: Annotated[ThreadsTable, Depends(runtime.threads_table)],
) -> AgentSessionMeta:
    return AgentSessionMeta(threads)


def thread_agents_index() -> ThreadAgentsIndex:
    """Агенты всех вкладок инстанса по тредам: один реестр на процесс."""
    return ThreadAgentsIndex()


def thread_deleter(
    launcher: Annotated[AgentLauncher, Depends(agent_launcher)],
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
) -> ThreadDeleter:
    return ThreadDeleter(launcher, index)


def agent_feeds(  # noqa: PLR0913 — фабрика собирается всеми входами процесса
    config: Annotated[RuntimeConfig, Depends(runtime.get_runtime_config)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    payloads: Annotated[PayloadStore, Depends(runtime.payload_store)],
    locks: Annotated[LiveLocks, Depends(runtime.live_locks)],
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    data_layer: Annotated[PostgresDataLayer, Depends(chainlit_data_layer)],
    surfaces: Annotated[TabSurfaces, Depends(thread_surfaces)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    texts: Annotated[UiText, Depends(feed_text)],
    agent: Annotated[AgentConfig, Depends(agent_config)],
) -> AgentFeeds:
    """Приёмники потока агентов: ход в шину под замком треда, история — в
    вкладку, оценки шагов — из слоя данных, вопросы агента — панелью вкладки
    со сроком [agent].question_timeout_sec."""
    parts = BusParts(
        bus=bus,
        payloads=payloads,
        locks=locks,
        heartbeat_sec=float(config.cluster.heartbeat_sec),
    )

    return AgentFeeds(
        parts,
        journals,
        servers,
        data_layer,
        surfaces,
        storage,
        texts,
        float(agent.question_timeout_sec),
    )


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
