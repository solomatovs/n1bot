"""Callback'и chainlit: мост между интерфейсом чата и агентом треда по ACP.

Ход — `session/prompt` агента вкладки, лента рисуется из его уведомлений через
шину; возврат к треду — `session/resume` после того, как chainlit отдал вкладке
сам тред; панель настроек — слой пользователя и слой чата агента, её правка
идёт единым путём AgentSettingsChanges с перезапуском процесса агента.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ClassVar
from uuid import UUID

import chainlit as cl
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
from fastapi import Request, Response

from boba.aid.agent.chat import (
    AgentSettingsChanges,
    Question,
    SessionOptions,
    SettingsOutcome,
    ThreadAgent,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadIdentity,
)
from boba.aid.agent.client import AcpClientError
from boba.aid.agent.config import AgentConfig
from boba.aid.agent.panel import AgentPanel
from boba.aid.agent.settings import (
    AgentSettingsError,
    AgentSettingsTable,
    ChatAgentSettings,
)
from boba.aid.canvas.panel import StreamActions
from boba.aid.canvas.remote import RemoteStreams
from boba.aid.canvas.tools import CanvasActions, CanvasScope
from boba.aid.chat.panel_text import PanelText
from boba.aid.domain.fields import ThreadField, ThreadMetaField
from boba.aid.infra import providers
from boba.aid.infra.config import AppConfig
from boba.aid.infra.providers import (
    agent_config,
    agent_settings_changes,
    agent_settings_table,
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    mcp_servers,
    session_profile,
    thread_agents,
    thread_agents_index,
)
from boba.aid.infra.session import (
    ChainlitSession,
    SessionContainers,
    current_session,
)
from boba.aid.infra.socket_events import SocketEvents
from boba.aid.infra.thread_room import (
    ChatRoomSurface,
    ThreadLive,
    ThreadRoom,
    UserRoom,
)
from boba.aid.rendering.errors import chainlit_error_ctx_handler
from boba.cancellation import StopReason
from boba.canvas.canvas import CanvasAction, RenderVerdicts
from boba.chat.profiles import ChatProfileConfig, ChatProfiles, SelectedProfile
from boba.chat.threads import DataLayerError
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError, RefusalError
from boba.identity.locks import LockBusyError
from boba.identity.run import Runs
from boba.identity.session import Login
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

logger = logging.getLogger(__name__)


class AgentAuthor:
    """Подпись сообщений об агенте в ленте: отказы и пометки о настройках."""

    NAME: ClassVar[str] = "Agent"


class SettingsNotice:
    """Пометка в ленте о сохранённых настройках, которые ждут конца хода."""

    DEFERRED: ClassVar[str] = (
        "Settings are saved and will apply after the current answer."
    )


AGENT_FAILURES = (
    AcpClientError,
    AgentSettingsError,
    McpClientError,
    LockBusyError,
)
"""Отказы пути агента, которые уходят в ленту сообщением об ошибке."""


@chainlit_error_ctx_handler
@di_inject
async def on_message(
    msg: cl.Message,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    config: Annotated[AgentConfig, Depends(agent_config)],
):
    """Ход по новому вопросу вкладки."""
    identity = ThreadIdentity.of(current_session(), selected)
    question = Question.of_message(msg, identity, config.public_url)
    await _run_turn(identity, question)


chainlit_config.code.on_message = wrap_user_function(on_message)


@chainlit_error_ctx_handler
@di_inject
async def on_edit(
    question: Question,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
):
    """Ход по правленому вопросу вкладки: тот же путь, что у нового вопроса;
    вложения правленого вопроса агент вкладки берёт из своей беседы."""
    identity = ThreadIdentity.of(current_session(), selected)
    await _run_turn(identity, question)


SocketEvents.on_edit(on_edit)


@di_inject
async def _run_turn(  # noqa: PLR0913, C901 — зависимости и ветки обработчика
    identity: ThreadIdentity,
    question: Question,
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
):
    """Ход по вопросу: агент вкладки получает `session/prompt`, лента рисуется
    из его уведомлений через шину; отказ агента или занятый тред уходят в
    ленту сообщением об ошибке, а при отказе правки вкладка сначала заново
    рисует беседу из истории — фронт уже обрезал её ленту; не вышло и это —
    в сообщении обе причины. Первая сессия
    треда открывает настройки сессии, а конец хода исполняет отложенный
    перезапуск агента — в обоих случаях панель присылается заново."""
    thread_id = identity.thread_id

    ThreadRoom.activate(thread_id)

    # рендерер треда подписан на область до первого сообщения хода
    ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

    try:
        agent = await agents.of(identity)
    except AGENT_FAILURES as exc:
        logger.error(
            "thread %s: agent of turn %s failed: %s", thread_id, question.key, exc
        )
        await cl.ErrorMessage(content=str(exc), author=AgentAuthor.NAME).send()
        return

    had_options = bool(agent.options.options)
    restarts = agent.restarts
    try:
        asked = question
        if question.edited:
            asked = agent.edited(question)

        await agent.ask(asked)
    except AGENT_FAILURES as exc:
        logger.error("thread %s: turn %s failed: %s", thread_id, question.key, exc)
        content = str(exc)
        # фронт уже обрезал ленту правящей вкладки; свой идущий ход не трогаем
        reload = question.edited
        if agent.turn_active():
            reload = False

        if reload:
            try:
                await agent.reload()
            except AGENT_FAILURES as redraw:
                logger.error(
                    "thread %s: redrawing the tab after turn %s failed: %s",
                    thread_id,
                    question.key,
                    redraw,
                )
                content = (
                    f"{exc}\n\nThe conversation was not redrawn after this: the "
                    f"agent did not replay it: {redraw}"
                )
            except DataLayerError as redraw:
                logger.error(
                    "thread %s: redrawing the tab after turn %s failed: %s",
                    thread_id,
                    question.key,
                    redraw,
                )
                content = (
                    f"{exc}\n\nThe conversation was not redrawn after this: the "
                    f"thread was not read: {redraw.view_message().content}"
                )
            except InternalServiceError as redraw:
                logger.error(
                    "thread %s: redrawing the tab after turn %s failed: %s",
                    thread_id,
                    question.key,
                    redraw.internal_detail,
                )
                content = (
                    f"{exc}\n\nThe conversation was not redrawn after this "
                    "because of an internal error; reload the thread."
                )

        await cl.ErrorMessage(content=content, author=AgentAuthor.NAME).send()
        return

    opened = False
    if not had_options:
        opened = bool(agent.options.options)

    if opened:
        await _send_settings(app_config, registry, table, agent)
        return

    if agent.restarts != restarts:
        await _send_settings(app_config, registry, table, agent)


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
        try:
            files = await self._servers.files(profile.mcp[0], caller)
        except McpClientError as exc:
            logger.warning("file upload is hidden for %s: %s", caller.login, exc)
            return False

        return files is not None


def _session_selected_profile(registry: ChatProfiles) -> SelectedProfile:
    session = current_session()

    return registry.resolve(session.chat_profile, session.sign_in)


class SettingsRefresh:
    """Чужая вкладка пользователя после сохранения настроек: если она на том же
    профиле — панель заново, со свежим слоем пользователя. Агент её треда не
    перезапускается: слой чата у каждого треда свой.
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

            agent = _tab_agent(session)
            await _send_settings(app_config, registry, table, agent)
        finally:
            context_var.reset(token)


UserRoom.on_settings(SettingsRefresh.apply)


async def _agent_panel(
    app_config: AppConfig,
    registry: ChatProfiles,
    table: AgentSettingsTable,
    agent: ThreadAgent | None,
) -> AgentPanel | None:
    """Вкладки про агента для текущей сессии: слой пользователя, слой чата
    треда и варианты выбора сессии у агента вкладки; None — сессия без
    пользователя."""
    session = current_session()
    user_id = session.user_id
    if user_id is None:
        return None

    selected = _session_selected_profile(registry)
    user = await table.user_layer(UUID(user_id), selected.name)
    chat: ChatAgentSettings | None = None
    if thread_id := session.thread_id:
        chat = await table.chat_layer(UUID(thread_id))

    options = SessionOptions()
    if agent is not None:
        options = agent.options

    return AgentPanel(
        selected.config,
        user,
        chat,
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
    panel = await _agent_panel(app_config, registry, table, agent)
    if panel is None:
        return []

    return panel.tabs()


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
    agents = container.resolved(thread_agents, scope="session")

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


@chainlit_error_ctx_handler
@di_inject
async def on_settings_update(  # noqa: PLR0913 — зависимости обработчика
    settings: dict[str, Any],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    changes: Annotated[AgentSettingsChanges, Depends(agent_settings_changes)],
):
    """Форма панели разбирается в правку настроек и уходит единому пути
    AgentSettingsChanges: слои записаны, процесс агента вкладки перезапущен
    либо ждёт конца идущего хода — тогда об этом пометка в ленте. Отказ
    разбора или перезапуска уходит в ленту сообщением об ошибке."""
    session = current_session()
    user_id = session.user_id
    if user_id is None:
        logger.warning(
            "settings update: chainlit session %r has no user, ignored", session.id
        )
        return

    selected = _session_selected_profile(registry)
    identity = ThreadIdentity.of(session, selected)
    agent = agents.of_thread(identity.thread_id)
    panel = await _agent_panel(app_config, registry, table, agent)
    if panel is None:
        return

    try:
        outcome = await changes.apply(identity, panel.parse(settings), agent)
    except AGENT_FAILURES as exc:
        logger.error(
            "thread %s: settings of profile %s are not applied: %s",
            identity.thread_id,
            selected.name,
            exc,
        )
        await cl.ErrorMessage(content=str(exc), author=AgentAuthor.NAME).send()
        return

    if outcome is SettingsOutcome.DEFERRED:
        await cl.Message(
            content=SettingsNotice.DEFERRED, author=AgentAuthor.NAME
        ).send()

    # панель показывает сохранённое; после перезапуска — с настройками сессии
    await _send_settings(app_config, registry, table, agent)

    # остальные вкладки пользователя узнают о настройках из его области
    changed = ChatSettingsChanged(profile=selected.name, by_session=session.id)
    await _root_bus().publish(Scope.user(UUID(user_id)), changed, LockToken.local())

    logger.info(
        "settings saved: thread=%s, profile=%s, outcome=%s",
        identity.thread_id,
        selected.name,
        outcome.value,
    )


# без task-обёртки chainlit: её task_end погасил бы индикатор идущего хода
chainlit_config.code.on_settings_update = wrap_user_function(on_settings_update)


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
    истории `session/resume` после того, как chainlit отдал вкладке тред (шагов в
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
