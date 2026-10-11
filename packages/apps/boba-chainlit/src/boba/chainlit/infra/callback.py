"""Callback'и chainlit: мост между интерфейсом чата и агентом треда по ACP.

Ход — `session/prompt` агента вкладки, лента рисуется из его уведомлений через
шину; возврат к треду — `session/load` после того, как chainlit отдал вкладке
сам тред; панель настроек — настройки LLM пользователя, слой пользователя для
агента и настройки открытой сессии.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, ClassVar
from uuid import UUID

from fastapi import Request, Response

import chainlit as cl
from boba.cancellation import StopReason
from boba.canvas.canvas import CanvasAction, RenderVerdicts
from boba.chainlit.acp.chat import (
    Question,
    SessionOptions,
    ThreadAgent,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadIdentity,
)
from boba.chainlit.acp.client import AcpClientError, AgentRequestError
from boba.chainlit.acp.panel import AgentPanel
from boba.chainlit.acp.settings import AgentSettingsError, AgentSettingsTable
from boba.chainlit.acp.tokens import McpTokenError, SessionServersError
from boba.chainlit.canvas.panel import StreamActions
from boba.chainlit.canvas.remote import RemoteStreams
from boba.chainlit.canvas.tools import CanvasActions, CanvasScope
from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.chat.settings import SettingsPanel
from boba.chainlit.data.data_layer import AttachmentDataLayer, PostgresDataLayer
from boba.chainlit.domain.fields import ThreadField, ThreadMetaField
from boba.chainlit.infra import providers
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.providers import (
    agent_settings_table,
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    mcp_servers,
    session_profile,
    thread_agents,
    thread_agents_index,
)
from boba.chainlit.infra.session import (
    ChainlitSession,
    SessionContainers,
    current_session,
)
from boba.chainlit.infra.thread_room import (
    ChatRoomSurface,
    ThreadLive,
    ThreadRoom,
    UserRoom,
)
from boba.chainlit.rendering.errors import chainlit_error_ctx_handler
from boba.chat.profiles import (
    ChatProfileConfig,
    ChatProfiles,
    SelectedProfile,
    SettingsView,
    UserLlmOverrides,
    UserMeta,
)
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError, RefusalError
from boba.identity.locks import LockBusyError
from boba.identity.run import Runs
from boba.identity.session import Login, UserMetadataField
from boba.identity.token import CookieSpec
from boba.mcp_client.client import McpCaller, McpClientError, McpServers
from boba.messaging import (
    ChatSettingsChanged,
    LockToken,
    MessageBus,
    StopRequested,
)
from boba.runtime import providers as runtime
from boba.runtime.config import RuntimeConfig
from boba.runtime.di import Container, Depends, di_inject
from boba.runtime.http import SessionCookie
from chainlit.config import (
    ChainlitConfigOverrides,
    FeaturesSettings,
    SpontaneousFileUploadFeature,
)
from chainlit.config import config as chainlit_config
from chainlit.context import ChainlitContext, context, context_var
from chainlit.data.base import BaseDataLayer
from chainlit.emitter import ChainlitEmitter
from chainlit.input_widget import Tab
from chainlit.session import WebsocketSession
from chainlit.types import ThreadDict
from chainlit.utils import wrap_user_function

logger = logging.getLogger(__name__)


class AgentAuthor:
    """Подпись сообщений об отказе агента в ленте."""

    NAME: ClassVar[str] = "Agent"


AGENT_FAILURES = (
    AcpClientError,
    AgentSettingsError,
    McpTokenError,
    McpClientError,
    SessionServersError,
    LockBusyError,
)
"""Отказы пути агента, которые уходят в ленту сообщением об ошибке."""


@chainlit_error_ctx_handler
@di_inject
async def on_message(  # noqa: PLR0913 — зависимости обработчика
    msg: cl.Message,
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
):
    """Ход по вопросу: агент вкладки получает `session/prompt`, лента рисуется
    из его уведомлений через шину; отказ агента или занятый тред уходят в
    ленту сообщением об ошибке. Первая сессия треда открывает настройки
    сессии — панель присылается заново."""
    session = current_session()
    identity = ThreadIdentity.of(session, selected)
    thread_id = identity.thread_id

    ThreadRoom.activate(thread_id)

    # рендерер треда подписан на область до первого сообщения хода
    ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

    question = Question.of_message(msg, thread_id, AttachmentDataLayer.require().links)
    try:
        agent = await agents.of(identity)
        had_options = bool(agent.options.options)
        await agent.ask(question)
    except AGENT_FAILURES as exc:
        logger.error("thread %s: turn %s failed: %s", thread_id, msg.id, exc)
        await cl.ErrorMessage(content=str(exc), author=AgentAuthor.NAME).send()
        return

    if not had_options and agent.options.options:
        await _send_settings(app_config, registry, table, agent)


chainlit_config.code.on_message = wrap_user_function(on_message)


def _root_bus() -> MessageBus:
    """Шина процесса из корневого контейнера для обработчиков без DI-инъекции."""
    root = Container.root
    if root is None:
        raise InternalServiceError(
            internal_detail=(
                "_root_bus: Container.root is not initialised, bootstrap has not run"
            ),
            user_detail=None,
        )

    return root.resolved(runtime.message_bus)


def _root_remote_streams() -> RemoteStreams:
    """Журналы вызовов MCP-серверов для обработчиков панели: реестр и клиент
    из корневого контейнера, читает их пользователь текущей сессии."""
    root = Container.root
    if root is None:
        raise InternalServiceError(
            internal_detail=(
                "_root_remote_streams: Container.root is not initialised, "
                "bootstrap has not run"
            ),
            user_detail=None,
        )

    caller = McpCaller(
        login=current_session().identifier, roles=current_session().roles
    )

    return RemoteStreams(
        root.resolved(providers.remote_journals),
        root.resolved(providers.mcp_servers),
        caller,
    )


@cl.set_chat_profiles
@di_inject
async def set_chat_profiles(
    user: cl.User | None,
    language: str | None,
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
) -> list[cl.ChatProfile]:
    """Профили, выданные входу пользователя; выбор профиля обязателен.
    Загрузка файлов включена в профиле, чей первый MCP-сервер объявил
    хранилище файлов: своего хранилища у чата нет."""
    granted = ChainlitSession.profiles_of(user)
    uploads = ProfileUploads(servers, user)

    profiles: list[cl.ChatProfile] = []
    for name, profile in registry.visible_for(granted).items():
        icon = None
        if profile.icon:
            icon = profile.icon

        profiles.append(
            cl.ChatProfile(
                name=name,
                display_name=profile.display_name,
                markdown_description=profile.description,
                icon=icon,
                default=profile.default,
                config_overrides=await uploads.overrides(profile),
            )
        )

    return profiles


class ProfileUploads:
    """Доступность загрузки файлов в профиле для пользователя.

    Создаётся обработчиком set_chat_profiles на один запрос настроек. Файлы
    пользователя лежат на первом MCP-сервере профиля; если сервера нет, он
    недоступен или хранилища файлов не объявил, кнопка загрузки в этом
    профиле скрыта.
    """

    def __init__(self, servers: McpServers, user: cl.User | None) -> None:
        self._servers = servers
        self._user = user

    async def overrides(self, profile: ChatProfileConfig) -> ChainlitConfigOverrides:
        upload = SpontaneousFileUploadFeature(enabled=await self.enabled(profile))

        return ChainlitConfigOverrides(
            features=FeaturesSettings(spontaneous_file_upload=upload)
        )

    async def enabled(self, profile: ChatProfileConfig) -> bool:
        user = self._user
        if user is None:
            return False

        if not profile.mcp:
            return False

        caller = McpCaller(
            login=Login(user.identifier), roles=ChainlitSession.roles_of(user)
        )
        files = await self._servers.files(profile.mcp[0], caller)

        return files is not None


def _session_selected_profile(registry: ChatProfiles) -> SelectedProfile:
    session = current_session()

    return registry.resolve(session.chat_profile, session.sign_in)


def _session_view(config: AppConfig, registry: ChatProfiles) -> SettingsView:
    """Итоговые настройки сессии: профиль плюс личные настройки пользователя."""
    selected = _session_selected_profile(registry)

    saved = UserMeta.of(current_session().metadata).overrides_for(selected.name)
    return SettingsView.of(selected.config, saved)


def _refresh_session_user_meta(profile: str, overrides: UserLlmOverrides) -> None:
    """Свежие настройки — в metadata пользователя сессии, без перелогина."""
    user = current_session().user
    if user is None:
        return

    metadata = dict(user.metadata or {})
    llm = dict(metadata.get(UserMetadataField.LLM) or {})

    stored = overrides.stored()
    if stored:
        llm[profile] = stored
    else:
        llm.pop(profile, None)

    metadata[UserMetadataField.LLM] = llm
    user.metadata = metadata


class SettingsRefresh:
    """Чужая вкладка пользователя после сохранения настроек: если она на том же
    профиле — свежие metadata в сессию и панель заново. Агент открытого треда
    не трогается: слой пользователя действует на новые треды.
    """

    @staticmethod
    async def apply(socket: WebsocketSession, profile: str) -> None:
        root = Container.root
        if root is None:
            raise InternalServiceError(
                internal_detail=(
                    "settings refresh: Container.root is not initialised, "
                    "bootstrap has not run"
                ),
                user_detail=None,
            )

        registry = root.resolved(chat_profiles_registry)
        app_config = root.resolved(get_app_config)
        table = root.resolved(agent_settings_table)
        token = context_var.set(ChainlitContext(socket, ChainlitEmitter(socket)))
        try:
            session = current_session()
            selected = registry.resolve(session.chat_profile, session.sign_in)
            if selected.name != profile:
                return

            await SettingsRefresh._adopt_metadata(
                session, root.resolved(chainlit_data_layer)
            )
            agent = _tab_agent(session)
            await _send_settings(app_config, registry, table, agent)
        finally:
            context_var.reset(token)

    @staticmethod
    async def _adopt_metadata(session: ChainlitSession, layer: BaseDataLayer) -> None:
        fresh = await layer.get_user(session.identifier)
        user = session.user
        if fresh is None or user is None:
            return

        user.metadata = dict(fresh.metadata or {})


UserRoom.on_settings(SettingsRefresh.apply)


def _llm_panel(app_config: AppConfig, registry: ChatProfiles) -> SettingsPanel:
    """Панель настроек LLM пользователя для текущей сессии."""
    return SettingsPanel(
        _session_view(app_config, registry),
        PanelText(app_config.chainlit.root, current_session().language),
    )


async def _agent_panel(
    app_config: AppConfig,
    registry: ChatProfiles,
    table: AgentSettingsTable,
    agent: ThreadAgent | None,
) -> AgentPanel | None:
    """Вкладки про агента для текущей сессии; None — сессия без пользователя."""
    session = current_session()
    user_id = session.user_id
    if user_id is None:
        return None

    selected = _session_selected_profile(registry)
    user = await table.user_layer(UUID(user_id), selected.name)
    options = SessionOptions()
    if agent is not None:
        options = agent.options

    return AgentPanel(
        selected.config,
        user,
        options,
        PanelText(app_config.chainlit.root, session.language),
    )


async def _session_tabs(
    app_config: AppConfig,
    registry: ChatProfiles,
    table: AgentSettingsTable,
    agent: ThreadAgent | None,
) -> list[Tab]:
    """Вкладки панели настроек сессии; пусто — профиль ничего не открывает."""
    tabs = _llm_panel(app_config, registry).tabs()
    panel = await _agent_panel(app_config, registry, table, agent)
    if panel is not None:
        tabs.extend(panel.tabs())

    return tabs


async def _send_settings(
    app_config: AppConfig,
    registry: ChatProfiles,
    table: AgentSettingsTable,
    agent: ThreadAgent | None,
) -> None:
    """Панель настроек вкладке; без вкладок панель не показывается."""
    if tabs := await _session_tabs(app_config, registry, table, agent):
        await cl.ChatSettings(tabs).send()


def _tab_agent(session: ChainlitSession) -> ThreadAgent | None:
    """Агент треда текущей вкладки; None — у вкладки нет треда или агента."""
    thread_id = session.thread_id
    if thread_id is None:
        return None

    container = SessionContainers.of(session.id)
    agents = container.resolved(thread_agents)

    return agents.of_thread(thread_id)


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
):
    session = current_session()
    logger.info(
        "chat start: session=%s, thread=%s, profile=%s, language=%s",
        session.id,
        session.thread_id,
        session.chat_profile or "none",
        session.language or "browser",
    )

    # вкладка присоединилась к треду: рендерер этого инстанса подписан на его область,
    # и ход, начатый на другом инстансе, рисуется здесь так же, как свой
    if thread_id := session.thread_id:
        ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

    if user_id := session.user_id:
        UserRoom.join(UUID(user_id))

    # профиль без разрешённых настроек панель не показывает
    await _send_settings(app_config, registry, table, None)

    # процесс агента поднимается вместе с вкладкой: первый вопрос его не ждёт
    if session.thread_id:
        await _start_agent(agents, registry, session)


async def _start_agent(
    agents: ThreadAgents, registry: ChatProfiles, session: ChainlitSession
) -> ThreadAgent | None:
    """Процесс агента под тред вкладки; без выбранного профиля ждём первого
    хода; None — агент не поднялся, причина в ленте."""
    try:
        selected = registry.resolve(session.chat_profile, session.sign_in)
    except RefusalError as exc:
        logger.info("thread %s: agent waits for a profile: %s", session.thread_id, exc)
        return None

    identity = ThreadIdentity.of(session, selected)
    try:
        return await agents.of(identity)
    except AGENT_FAILURES as exc:
        logger.error("thread %s: agent start failed: %s", identity.thread_id, exc)
        await cl.ErrorMessage(content=str(exc), author=AgentAuthor.NAME).send()

        return None


@cl.on_settings_update
@chainlit_error_ctx_handler
@di_inject
async def on_settings_update(  # noqa: PLR0913 — зависимости обработчика
    settings: dict[str, Any],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
):
    """Сохраняет настройки LLM пользователя (users.meta) и слой пользователя
    агента; настройки открытой сессии уходят агенту треда
    (`session/set_config_option`), его отказ — в ленту."""
    selected = _session_selected_profile(registry)
    session = current_session()
    view = _llm_panel(app_config, registry).parse(settings)
    overrides = view.overrides

    user_id = session.user_id
    if user_id is None:
        logger.warning(
            "settings update: chainlit session %r has no user, ignored", session.id
        )
        return

    if not isinstance(data_layer, PostgresDataLayer):
        msg = (
            "settings update expects a PostgresDataLayer data layer, "
            f"got {type(data_layer).__name__}"
        )
        raise RuntimeError(msg)

    await data_layer.update_user_llm_settings(
        UUID(user_id), selected.name, overrides.stored()
    )
    _refresh_session_user_meta(selected.name, overrides)

    agent: ThreadAgent | None = None
    if thread_id := session.thread_id:
        agent = agents.of_thread(thread_id)

    panel = await _agent_panel(app_config, registry, table, agent)
    if panel is not None:
        edit = panel.parse(settings)
        system_prompt = None
        if overrides.user_prompt:
            system_prompt = view.agent().system_prompt

        user = edit.user.model_copy(update={"system_prompt": system_prompt})
        await table.save_user_layer(UUID(user_id), selected.name, user)
        await _apply_session_options(agent, edit.session)

    # остальные вкладки пользователя узнают о настройках из его области
    changed = ChatSettingsChanged(profile=selected.name, by_session=session.id)
    await _root_bus().publish(Scope.user(UUID(user_id)), changed, LockToken.local())

    logger.info(
        "settings saved: profile=%s, overrides=%s",
        selected.name,
        sorted(overrides.stored()),
    )


async def _apply_session_options(
    agent: ThreadAgent | None, changed: Mapping[str, str]
) -> None:
    """Изменённые настройки сессии — агенту треда; без агента или сессии
    настройки ждут её открытия, отказ агента (идёт ход) показывается в чат."""
    if agent is None:
        return

    for config_id, value in changed.items():
        try:
            applied = await agent.set_option(config_id, value)
        except AgentRequestError as exc:
            logger.warning(
                "thread %s: option %s=%s refused: %s",
                agent.thread_id,
                config_id,
                value,
                exc,
            )
            await cl.ErrorMessage(content=str(exc), author=AgentAuthor.NAME).send()
            continue

        if not applied:
            logger.info(
                "thread %s: option %s=%s waits for the session",
                agent.thread_id,
                config_id,
                value,
            )


@cl.on_logout
def on_logout(request: Request, response: Response):
    # только свои: на домене живут и чужие приложения, а среди присланных
    # кук попадаются имена, которых http.cookies не принимает ('Path')
    _session_cookie().clear(response, request.cookies)


def _session_cookie() -> SessionCookie:
    """Cookie входа по [session] из корневого контейнера."""
    root = Container.root
    if root is None:
        raise InternalServiceError(
            internal_detail=(
                "session cookie: Container.root is not initialised, "
                "bootstrap has not run"
            ),
            user_detail=None,
        )

    config = root.resolved(runtime.get_runtime_config)
    if not isinstance(config, RuntimeConfig):
        raise InternalServiceError(
            internal_detail=(
                "session cookie expects RuntimeConfig from the config provider, "
                f"got {type(config).__name__}"
            ),
            user_detail=None,
        )

    session = config.session
    spec = CookieSpec(
        name=session.cookie,
        samesite=session.cookie_samesite,
        ttl_sec=session.session_ttl_sec,
    )

    return SessionCookie(spec)


@cl.on_stop
@di_inject
async def on_stop(
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    instance: Annotated[str, Depends(runtime.instance_name)],
    runs: Annotated[Runs, Depends(runtime.runs)],
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
):
    """Кнопка Stop: ход агента любой вкладки этого инстанса получает
    `session/cancel`, чужой ход — команду через шину."""
    session = current_session()
    thread_id = session.thread_id
    if thread_id is None:
        return

    if agent := index.active(thread_id):
        await agent.stop()
        return

    if runs.stop(thread_id, StopReason.USER_STOP):
        return

    user_id = session.user_id
    if user_id is None:
        logger.info("stop pressed for thread %s without a user", thread_id)
        return

    command = StopRequested(by_user=UUID(user_id), by_instance=instance)
    command_id = await bus.command(Scope.chat(thread_id), command)
    logger.info("stop of thread %s sent as command %d", thread_id, command_id)


@cl.data_layer
@di_inject
def get_data_layer(
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
) -> BaseDataLayer:
    return data_layer


def _session_canvas_scope() -> CanvasScope | None:
    """Чьи файлы показывает панель — по сессии; None — сессия без треда или входа."""
    thread_id = current_session().thread_id
    if thread_id is None:
        return None

    user_id = current_session().user_id
    if user_id is None:
        return None

    return CanvasScope(user_id=str(user_id), thread_id=thread_id)


@cl.action_callback(CanvasAction.OPEN)
@chainlit_error_ctx_handler
async def on_canvas_open(action: cl.Action) -> None:
    """Клик по ссылке в переписке открывает панель без участия агента."""
    scope = _session_canvas_scope()
    if scope is None:
        logger.warning("canvas open without a thread session: %s", action.payload)
        return

    await CanvasActions.open(action, scope)


@cl.action_callback(CanvasAction.CONTENT)
@chainlit_error_ctx_handler
async def on_canvas_content(action: cl.Action) -> dict[str, Any]:
    """Панель уже открыта: отдаём описание файла, не подменяя элемент."""
    scope = _session_canvas_scope()
    if scope is None:
        return {}

    return await CanvasActions.content(action, scope)


@cl.action_callback(CanvasAction.SHOW)
@chainlit_error_ctx_handler
async def on_canvas_stream(action: cl.Action) -> dict[str, Any]:
    """Кнопка на шаге инструмента: журнал вызова в панель плюс слежение.

    Открытая панель просит содержимое ответом (inline) — тогда элемент не
    пушится и панель не переоткрывается.

    Пользователь и тред берутся из сессии: чужой журнал по payload недостижим.
    """
    thread_id = current_session().thread_id
    if thread_id is None:
        return {}

    user_id = current_session().user_id
    if user_id is None:
        return {}

    logger.info(
        "stream show: user=%s thread=%s payload=%s",
        user_id,
        thread_id,
        dict(action.payload),
    )

    return await StreamActions.show(
        str(user_id), thread_id, action.payload, _root_remote_streams()
    )


@cl.action_callback(CanvasAction.WINDOW)
@chainlit_error_ctx_handler
async def on_canvas_stream_window(action: cl.Action) -> dict[str, Any]:
    """Окно журнала или файла по смещению: панель не подменяется."""
    thread_id = current_session().thread_id
    if thread_id is None:
        return {}

    user_id = current_session().user_id
    if user_id is None:
        return {}

    return await StreamActions.window(
        str(user_id), thread_id, action.payload, _root_remote_streams()
    )


@cl.action_callback(CanvasAction.LEAVE)
@chainlit_error_ctx_handler
async def on_canvas_leave(action: cl.Action) -> None:
    """Панель закрыта или сменила файл: слежение прежнего показа снимается."""
    thread_id = current_session().thread_id
    if thread_id is None:
        return

    StreamActions.leave(thread_id, action.payload)


@cl.action_callback(CanvasAction.STATUS)
@chainlit_error_ctx_handler
async def on_canvas_render_status(action: cl.Action) -> None:
    """Отчёт браузера об исходе рендера: его ждёт вьювер по nonce."""
    RenderVerdicts.report(action.payload)


@cl.on_chat_resume
@chainlit_error_ctx_handler
@di_inject
async def on_chat_resume(
    thread_dict: ThreadDict,
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
):
    """Вкладка вернулась к треду.

    Профиль из meta треда, которого нет в конфиге или который не выдан входу,
    заменяется профилем по умолчанию до сборки агента сессии: иначе агент не
    собирается, а фронт без конца переключает профиль. Этот же thread_dict
    chainlit следом отправит фронту, а профиль сессии запишет в meta на
    disconnect.
    """
    session = current_session()
    sign_in = session.sign_in

    retired = False
    if stored := session.chat_profile:
        retired = stored not in registry.visible_for(sign_in.profiles)

    if retired:
        fallback = registry.resolve_or_default(None, sign_in)
        context.session.chat_profile = fallback.name

        if metadata := thread_dict[ThreadField.METADATA]:
            metadata[ThreadMetaField.CHAT_PROFILE] = fallback.name

        logger.warning(
            "resume thread %s: chat profile %r is not granted, switched to %r",
            thread_dict[ThreadField.ID],
            stored,
            fallback.name,
        )
        notice = (
            f'Chat profile "{stored}" is no longer available: '
            f'this chat continues with "{fallback.config.display_name}"'
        )
        await context.emitter.send_toast(notice, "warning")

    await _resume_feed(thread_dict)


@di_inject
async def _resume_feed(
    thread_dict: ThreadDict,
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
):
    """Вкладка вернулась к треду: комнаты, агент вкладки и воспроизведение
    истории `session/load` после того, как chainlit отдал вкладке тред (шагов в
    нём нет: их рисует воспроизведение), затем панель настроек с настройками
    сессии."""
    thread_id = thread_dict[ThreadField.ID]
    ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)
    if user_id := current_session().user_id:
        UserRoom.join(UUID(user_id))

    room: list[str] = []
    for session in ThreadRoom.sessions(thread_id):
        room.append(session.id)

    logger.info(
        "resume thread %s: thread sessions=%s, current session=%s",
        thread_id,
        room,
        current_session().id,
    )

    session = current_session()
    socket = ThreadRoom.websocket()
    agent = await _start_agent(agents, registry, session)

    async def after_resume() -> None:
        if agent is not None:
            await _load_agent(agent)

        await _follow_live_turn(socket, thread_id)
        await _send_settings(app_config, registry, table, agent)

    ResumeEmitter.install(socket, after_resume)


async def _follow_live_turn(socket: WebsocketSession, thread_id: str) -> None:
    """Ход треда идёт где-то ещё: вкладка получает его открытые шаги и
    индикатор хода. Рендерер этого инстанса либо уже ведёт ход (другая
    вкладка), либо догоняет его по сохранённым сообщениям шины."""
    renderer = ChatRoomSurface.renderer_of(socket, thread_id)
    emitter = ChainlitEmitter(socket)
    if renderer.turn_alive:
        for step in renderer.resume_steps():
            await emitter.send_step(step)

        await emitter.task_start()
        return

    if not await ThreadLive.turn_alive(thread_id):
        return

    caught = await renderer.catch_up(_root_bus())
    logger.info("resume thread %s: foreign turn, caught up: %s", thread_id, caught)
    if caught.alive:
        await emitter.task_start()


class ResumeEmitter(ChainlitEmitter):
    """Эмиттер обработчика возврата к треду: после отправки треда вкладке
    (resume_thread) исполняет отложенное — воспроизведение истории и панель.
    Раньше отправки шаги воспроизведения пропали бы: фронт заменяет ленту
    шагами треда."""

    def __init__(
        self, session: WebsocketSession, after: Callable[[], Awaitable[None]]
    ) -> None:
        super().__init__(session)
        self._after = after

    async def resume_thread(self, thread_dict: ThreadDict) -> None:
        await super().resume_thread(thread_dict)
        await self._after()

    @classmethod
    def install(
        cls, session: WebsocketSession, after: Callable[[], Awaitable[None]]
    ) -> None:
        """Подменяет эмиттер в контексте текущего подключения: chainlit держит
        этот объект контекста локально и зовёт его resume_thread сразу после
        обработчика, поэтому меняется эмиттер объекта, а не сам контекст."""
        context_var.get().emitter = cls(session, after)


async def _load_agent(agent: ThreadAgent) -> None:
    """Агент вкладки загружает сессию треда с воспроизведением истории в
    ленту; отказ агента уходит в ленту."""
    try:
        loaded = await agent.resume()
    except AGENT_FAILURES as exc:
        logger.error("thread %s: agent resume failed: %s", agent.thread_id, exc)
        await cl.ErrorMessage(content=str(exc), author=AgentAuthor.NAME).send()
        return

    if not loaded:
        logger.info(
            "thread %s: no agent session yet, a new one opens on the first turn",
            agent.thread_id,
        )
