"""Провайдеры процесса aid: конфиг чата, data layer, клиент MCP чата
(файлы, журналы, карточки инструментов), процессы агента ACP и приёмники
его потока.

Общие для процессов провайдеры (реестр, сторы) — boba.runtime.providers.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Annotated
from uuid import UUID

from chainlit.config import config as chainlit_config

from boba.aid.agent.chat import (
    AgentLauncher,
    AgentSessionMeta,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadDeleter,
)
from boba.aid.agent.commands import CommandMenus
from boba.aid.agent.config import AgentConfig, AgentSettings
from boba.aid.agent.feed import AgentFeeds, BusParts
from boba.aid.agent.relay import McpRegistrations, McpRelay
from boba.aid.canvas.remote import RemoteJournals
from boba.aid.canvas.tools import CanvasOpener
from boba.aid.chat.ui_text import UiText
from boba.aid.data import PostgresDataLayer
from boba.aid.data.remote_storage import FileOwners, RemoteStorageClient
from boba.aid.infra.config import AppConfig
from boba.aid.infra.sandbox import SandboxLinks
from boba.aid.infra.session import (
    ChainlitSessions,
    current_session,
    session_source_ref,
)
from boba.aid.mcp_servers import McpAppError
from boba.aid.mcp_servers.catalog import McpConnectionCatalogs
from boba.aid.mcp_servers.directory import McpDirectory
from boba.aid.mcp_servers.form import McpForms
from boba.aid.mcp_servers.oauth import McpBrowserOAuth
from boba.aid.mcp_servers.oauth_store import McpOAuthStore
from boba.aid.mcp_servers.sources import ServerSource, StoredServers
from boba.aid.mcp_servers.store import McpServerStore
from boba.aid.rendering.renderer import TabSurfaces
from boba.auth import JwtTokens
from boba.chat.profiles import ChatProfiles, SelectedProfile
from boba.connection_broker.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import UserRows
from boba.identity.context import Scope
from boba.identity.locks import LiveLocks
from boba.identity.session import SessionSource
from boba.mcp_client.client import McpServers
from boba.messaging import (
    Envelope,
    LockToken,
    McpServersChanged,
    MessageBus,
    MessageBusError,
    PayloadStore,
)
from boba.runtime import providers as runtime
from boba.runtime.config import InstanceData, RuntimeConfig
from boba.runtime.di import Container, Depends
from boba.runtime.elements import FeedbacksTable
from boba.runtime.storage import StorageClient
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable


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


def sandbox_links(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> SandboxLinks:
    """Песочница приложений MCP Apps процесса: адрес с токеном для ленты и
    токен для страницы-прокси. Одна на процесс."""
    return SandboxLinks(app_config.server)


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


def server_source_ref() -> ServerSource:
    """Источник серверов зрителя из корневого контейнера; зовётся на каждую
    операцию с файлами."""
    return Container.require_root("chat providers").resolved(server_source)


def file_owners() -> FileOwners:
    """Владельцы файлов: по ключу объекта — пользователь и выданные ему
    серверы."""
    return FileOwners(session_source_ref, user_rows_ref, server_source_ref)


def storage_provider(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    owners: Annotated[FileOwners, Depends(file_owners)],
) -> StorageClient:
    """Хранилище вложений чата: файлы лежат в workspace пользователя на
    MCP-сервере его профиля."""
    return RemoteStorageClient(app_config.server.url_prefix, owners, mcp_servers_ref)


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

    return UiText(Path(chainlit_config.root), language)


def remote_journals() -> RemoteJournals:
    """Адреса журналов вызовов, исполненных MCP-серверами, на процесс:
    лента кладёт их по итогам вызовов агента."""
    return RemoteJournals()


async def chainlit_data_layer(  # noqa: PLR0913 — слой данных собирается таблицами процесса
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    threads: Annotated[ThreadsTable, Depends(runtime.threads_table)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
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


def mcp_server_cipher(
    config: Annotated[AppConfig, Depends(get_app_config)],
) -> SecretCipher:
    """Ключ шифрования учётных данных каталога mcp_servers."""
    return SecretCipher(config.mcp_servers.cipher.get_secret_value())


def mcp_server_store(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
    cipher: Annotated[SecretCipher, Depends(mcp_server_cipher)],
) -> McpServerStore:
    """Хранилище MCP-серверов процесса: таблица mcp_servers и выдачи grants."""
    return McpServerStore(pool, app_config.db_schema, cipher)


def mcp_oauth_store(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    pool: Annotated[AsyncPostgresPool, Depends(runtime.postgres_pool)],
    cipher: Annotated[SecretCipher, Depends(mcp_server_cipher)],
) -> McpOAuthStore:
    return McpOAuthStore(pool, app_config.db_schema, cipher)


def mcp_browser_oauth(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    store: Annotated[McpOAuthStore, Depends(mcp_oauth_store)],
) -> McpBrowserOAuth:
    redirect_uri = (
        str(app_config.server.public_url).rstrip("/") + "/api/mcp/oauth/callback"
    )
    return McpBrowserOAuth(store, redirect_uri)


def server_source(
    store: Annotated[McpServerStore, Depends(mcp_server_store)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    oauth: Annotated[McpBrowserOAuth, Depends(mcp_browser_oauth)],
) -> ServerSource:
    """Источник серверов зрителя: набор по выдаче и конфиги для клиента."""
    return StoredServers(store, users, oauth)


async def mcp_servers(
    source: Annotated[ServerSource, Depends(server_source)],
) -> AsyncIterator[McpServers]:
    """MCP-серверы процесса: подключаются при первом обращении, закрываются
    на остановке. Через них идут вызовы агента (ретранслятор), файлы
    серверов, журналы вызовов и инструменты для ленты."""
    servers = McpServers(source)
    try:
        yield servers
    finally:
        await servers.stop()


def agent_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> AgentSettings:
    return app_config.agent


def mcp_registrations() -> McpRegistrations:
    """Регистрации серверов процессов агента: один реестр на инстанс чата."""
    return McpRegistrations()


def canvas_opener(
    surfaces: Annotated[TabSurfaces, Depends(thread_surfaces)],
) -> CanvasOpener:
    """Показ файлов в панели канваса: по клику и по ответу tools/call."""
    return CanvasOpener(surfaces)


def mcp_relay(
    registrations: Annotated[McpRegistrations, Depends(mcp_registrations)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    canvas: Annotated[CanvasOpener, Depends(canvas_opener)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
) -> McpRelay:
    """Ретранслятор mcp/message процессов агента: один на инстанс; сервер
    chat — собственные инструменты чата из реестра; файлы ответа, которые
    сервер просит показать, открывает панель канваса, журналы идущих вызовов
    называет ленте шина."""
    own = version("boba-aid")

    return McpRelay(registrations, servers, canvas, bus, own)


def command_menus(
    relay: Annotated[McpRelay, Depends(mcp_relay)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    source: Annotated[ServerSource, Depends(server_source)],
) -> CommandMenus:
    """Реестры команд «/» вкладок: приложения серверов через ретранслятор и
    соединения пользователя из API MCP; команды чата выводят в ленту
    свои приложения."""
    return CommandMenus(relay, McpConnectionCatalogs(servers, source))


def thread_agents_index() -> ThreadAgentsIndex:
    """Агенты всех вкладок инстанса по тредам: один реестр на процесс."""
    return ThreadAgentsIndex()


logger = logging.getLogger(__name__)


class McpUpdates:
    """Доставляет изменение своих MCP-серверов агентам на всех узлах aid.

    Обработчики панели помечают местных агентов и публикуют имена в область
    пользователя; UserRoom принимает событие на остальных экземплярах.
    """

    def __init__(
        self,
        bus: MessageBus,
        index: ThreadAgentsIndex,
        servers: McpServers,
        instance: str,
    ) -> None:
        self._bus = bus
        self._index = index
        self._servers = servers
        self._instance = instance

    async def changed(self, user_id: UUID, names: Sequence[str]) -> None:
        self._index.mcp_changed(user_id)
        changed = McpServersChanged(servers=tuple(names))
        try:
            await self._bus.publish(Scope.user(user_id), changed, LockToken.local())
        except MessageBusError as exc:
            raise McpAppError(
                f"MCP server change was saved for user {user_id}, "
                f"but notifying other chats failed: {exc}"
            ) from exc

    async def received(self, user_id: UUID, envelope: Envelope) -> None:
        if envelope.origin == self._instance:
            return

        message = envelope.message
        if not isinstance(message, McpServersChanged):
            return

        self._index.mcp_changed(user_id)
        for name in message.servers:
            await self._servers.invalidate(name)

        logger.info(
            "MCP server update for user %s from %s: %s",
            user_id,
            envelope.origin,
            message.servers,
        )


def mcp_updates(
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    instance: Annotated[str, Depends(runtime.instance_name)],
) -> McpUpdates:
    """Изменения MCP-серверов между агентами пользователя на узлах чата."""
    return McpUpdates(bus, index, servers, instance)


def mcp_forms(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> McpForms:
    """Разбор запросов панели и сроки новых серверов из настроек aid."""
    return McpForms(app_config.mcp_servers)


def mcp_directory(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    source: Annotated[ServerSource, Depends(server_source)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
) -> McpDirectory:
    """Операции приложения MCP-серверов над клиентом MCP: один на процесс."""
    return McpDirectory(source, servers, app_config.mcp_servers)


def agent_launcher(  # noqa: PLR0913 — запуск собирается всеми входами процесса
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    config: Annotated[AgentSettings, Depends(agent_config)],
    source: Annotated[ServerSource, Depends(server_source)],
    profiles: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    registrations: Annotated[McpRegistrations, Depends(mcp_registrations)],
    relay: Annotated[McpRelay, Depends(mcp_relay)],
) -> AgentLauncher:
    """Запуск процессов агента: регистрации серверов, выданных пользователю
    треда, и сервера чата, файл настроек по текущему профилю, рукопожатие;
    mcp/message процессов отвечает ретранслятор. Профили конфига и строки
    users нужны удалению сессии треда. Приватные каталоги процессов агента —
    agents/ данных экземпляра."""
    return AgentLauncher(
        config,
        source,
        profiles,
        users,
        registrations,
        relay,
        version("boba-aid"),
        app_config.paths.data(InstanceData.AGENTS),
    )


def agent_session_meta(
    threads: Annotated[ThreadsTable, Depends(runtime.threads_table)],
) -> AgentSessionMeta:
    return AgentSessionMeta(threads)


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
    sandbox: Annotated[SandboxLinks, Depends(sandbox_links)],
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
        sandbox.app_url(),
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
    menus: Annotated[CommandMenus, Depends(command_menus)],
) -> AsyncIterator[ThreadAgents]:
    """Агенты вкладки: процессы живут с DI-контейнером сессии chainlit и
    закрываются вместе с ним."""
    agents = ThreadAgents(launcher, meta, index, feeds, menus)
    try:
        yield agents
    finally:
        await agents.close_all()
