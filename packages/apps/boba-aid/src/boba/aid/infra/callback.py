"""Callback'и chainlit: мост между интерфейсом чата и агентом треда по ACP.

Ход — `session/prompt` агента вкладки, лента рисуется из его уведомлений через
шину; возврат к треду — `session/resume` после того, как chainlit отдал вкладке
сам тред; панель настроек — слой пользователя и слой чата агента, её правка
идёт единым путём AgentSettingsChanges с перезапуском процесса агента;
приложения MCP Apps у шагов вызова зовут свой сервер действиями mcp_app_*.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ClassVar
from uuid import UUID

import chainlit as cl
import mcp_types as mt
from chainlit.config import (
    ChainlitConfigOverrides,
    FeaturesSettings,
    SpontaneousFileUploadFeature,
)
from chainlit.config import config as chainlit_config
from chainlit.context import ChainlitContext, context, context_var
from chainlit.data.base import BaseDataLayer
from chainlit.emitter import ChainlitEmitter
from chainlit.session import WebsocketSession
from chainlit.types import ThreadDict
from chainlit.utils import wrap_user_function
from fastapi import Request, Response

from boba.aid.agent.chat import (
    AgentSettingsChanges,
    ModelContextUpdate,
    Question,
    Questions,
    SessionOptions,
    SettingsOutcome,
    ThreadAgent,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadIdentities,
    ThreadIdentity,
)
from boba.aid.agent.client import AcpClientError
from boba.aid.agent.config import AgentConfig
from boba.aid.agent.panel import AgentPanel
from boba.aid.agent.relay import AppAction, AppRequest, McpRelay
from boba.aid.agent.settings import (
    AgentSettingsError,
    AgentSettingsTable,
    ChatAgentSettings,
)
from boba.aid.canvas.panel import StreamActions
from boba.aid.canvas.remote import RemoteJournals, RemoteStreams
from boba.aid.canvas.tools import CanvasOpener
from boba.aid.chat.panel_text import PanelText
from boba.aid.domain.fields import ThreadField, ThreadMetaField
from boba.aid.infra.config import AppConfig
from boba.aid.infra.providers import (
    agent_config,
    agent_settings_changes,
    agent_settings_table,
    canvas_opener,
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    mcp_relay,
    mcp_servers,
    remote_journals,
    session_profile,
    thread_agents,
    thread_agents_index,
    tool_media,
)
from boba.aid.infra.session import (
    ChainlitSession,
    SessionContainers,
    current_session,
)
from boba.aid.infra.socket_events import SocketEvents
from boba.aid.infra.thread_room import (
    ChatNotices,
    ChatRoomSurface,
    ThreadLive,
    ThreadRoom,
    UserRoom,
)
from boba.aid.rendering.acp_view import ToolMedia
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


class TabSettings:
    """Панель настроек агента вкладки: вкладки из слоя пользователя, слоя
    чата треда и вариантов сессии у агента вкладки.

    Создают обработчики chainlit из зависимостей DI на один вызов: панель
    шлётся при старте вкладки, после хода, открывшего сессию или
    перезапустившего агента, после правки настроек и после возврата к треду.
    """

    NOTICE_DEFERRED: ClassVar[str] = (
        "Settings are saved and will apply after the current answer."
    )
    """Пометка в ленте о сохранённых настройках, которые ждут конца хода."""

    def __init__(
        self, app_config: AppConfig, registry: ChatProfiles, table: AgentSettingsTable
    ) -> None:
        self._app_config = app_config
        self._registry = registry
        self._table = table

    def selected(self) -> SelectedProfile:
        """Профиль текущей сессии.

        Ошибки:
        RefusalError — профиль не выбран или недоступен ролям пользователя.
        """
        session = current_session()

        return self._registry.resolve(session.chat_profile, session.sign_in)

    async def panel(self, agent: ThreadAgent | None) -> AgentPanel | None:
        """Панель текущей сессии: слой пользователя, слой чата треда и
        варианты выбора сессии у агента вкладки; None — сессия без
        пользователя."""
        session = current_session()
        user_id = session.user_id
        if user_id is None:
            return None

        selected = self.selected()
        user = await self._table.user_layer(UUID(user_id), selected.name)
        chat: ChatAgentSettings | None = None
        if thread_id := session.thread_id:
            chat = await self._table.chat_layer(UUID(thread_id))

        options = SessionOptions()
        if agent is not None:
            options = agent.options

        return AgentPanel(
            selected.config,
            user,
            chat,
            options,
            PanelText(self._app_config.aid.root, session.language),
        )

    async def send(self, agent: ThreadAgent | None) -> None:
        """Панель вкладке; профиль, который ничего не открывает, панели не
        получает."""
        panel = await self.panel(agent)
        if panel is None:
            return

        if tabs := panel.tabs():
            await cl.ChatSettings(tabs).send()


class TabAgent:
    """Агент треда вкладки в обработчиках chainlit: поднять под тред,
    загрузить историю, провести ход. Отказы пути агента (FAILURES) уходят в
    ленту сообщением об ошибке, а не в общий обработчик ошибок.

    Создают обработчики из агентов сессии и профилей на один вызов; вопрос и
    личность вкладки собирают фабрики в его полях.
    """

    AUTHOR: ClassVar[str] = "Agent"
    """Подпись сообщений об агенте в ленте: отказы и пометки о настройках."""

    FAILURES: ClassVar[tuple[type[Exception], ...]] = (
        AcpClientError,
        AgentSettingsError,
        McpClientError,
        LockBusyError,
    )
    """Отказы пути агента, которые уходят в ленту сообщением об ошибке."""

    def __init__(self, agents: ThreadAgents, registry: ChatProfiles) -> None:
        self._agents = agents
        self._registry = registry
        self._identities = ThreadIdentities()
        self._questions = Questions()

    def identity(self, profile: SelectedProfile) -> ThreadIdentity:
        """Личность вкладки текущей сессии с профилем profile."""
        return self._identities.of(current_session(), profile)

    def question(
        self, msg: cl.Message, identity: ThreadIdentity, public_url: str
    ) -> Question:
        return self._questions.of_message(msg, identity, public_url)

    async def failed(self, content: str) -> None:
        """Отказ пути агента в ленту сообщением об ошибке."""
        await cl.ErrorMessage(content=content, author=self.AUTHOR).send()

    async def start(self, session: ChainlitSession) -> ThreadAgent | None:
        """Процесс агента под тред вкладки; без выбранного профиля ждём первого
        хода; None — агент не поднялся, причина в ленте."""
        try:
            selected = self._registry.resolve(session.chat_profile, session.sign_in)
        except RefusalError as exc:
            logger.info(
                "thread %s: agent waits for a profile: %s", session.thread_id, exc
            )
            return None

        identity = self._identities.of(session, selected)
        try:
            return await self._agents.of(identity)
        except self.FAILURES as exc:
            logger.error("thread %s: agent start failed: %s", identity.thread_id, exc)
            await self.failed(str(exc))

            return None

    async def load(self, agent: ThreadAgent) -> None:
        """Агент вкладки загружает сессию треда с воспроизведением истории в
        ленту; отказ агента уходит в ленту."""
        try:
            loaded = await agent.resume()
        except self.FAILURES as exc:
            logger.error("thread %s: agent resume failed: %s", agent.thread_id, exc)
            await self.failed(str(exc))
            return

        if not loaded:
            logger.info(
                "thread %s: no agent session yet, a new one opens on the first turn",
                agent.thread_id,
            )

    async def run(
        self, identity: ThreadIdentity, question: Question, settings: TabSettings
    ) -> None:
        """Ход по вопросу: агент вкладки получает `session/prompt`, лента
        рисуется из его уведомлений через шину; отказ агента или занятый тред
        уходят в ленту сообщением об ошибке, а при отказе правки вкладка
        сначала заново рисует беседу из истории — фронт уже обрезал её ленту;
        не вышло и это — в сообщении обе причины. Первая сессия треда
        открывает настройки сессии, а конец хода исполняет отложенный
        перезапуск агента — в обоих случаях панель присылается заново."""
        thread_id = identity.thread_id

        ThreadRoom.activate(thread_id)

        # рендерер треда подписан на область до первого сообщения хода
        ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

        try:
            agent = await self._agents.of(identity)
        except self.FAILURES as exc:
            logger.error(
                "thread %s: agent of turn %s failed: %s", thread_id, question.key, exc
            )
            await self.failed(str(exc))
            return

        had_options = bool(agent.options.options)
        restarts = agent.restarts
        try:
            asked = question
            if question.edited:
                asked = agent.edited(question)

            await agent.ask(asked)
        except self.FAILURES as exc:
            logger.error("thread %s: turn %s failed: %s", thread_id, question.key, exc)
            await self.failed(await self._redrawn(agent, question, exc))
            return

        opened = False
        if not had_options:
            opened = bool(agent.options.options)

        if opened:
            await settings.send(agent)
            return

        if agent.restarts != restarts:
            await settings.send(agent)

    async def _redrawn(
        self, agent: ThreadAgent, question: Question, failure: Exception
    ) -> str:
        """Текст отказа хода; у правки вкладка сначала заново рисует беседу из
        истории — фронт уже обрезал её ленту, а свой идущий ход не трогается.
        Не вышло и это — в тексте обе причины."""
        thread_id = agent.thread_id
        if not question.edited:
            return str(failure)

        if agent.turn_active():
            return str(failure)

        try:
            await agent.reload()
        except self.FAILURES as redraw:
            reason = f"the agent did not replay it: {redraw}"
            logged = str(redraw)
        except DataLayerError as redraw:
            reason = f"the thread was not read: {redraw.view_message().content}"
            logged = str(redraw)
        except InternalServiceError as redraw:
            logger.error(
                "thread %s: redrawing the tab after turn %s failed: %s",
                thread_id,
                question.key,
                redraw.internal_detail,
            )
            return (
                f"{failure}\n\nThe conversation was not redrawn after this "
                "because of an internal error; reload the thread."
            )
        else:
            return str(failure)

        logger.error(
            "thread %s: redrawing the tab after turn %s failed: %s",
            thread_id,
            question.key,
            logged,
        )

        return f"{failure}\n\nThe conversation was not redrawn after this: {reason}"


@chainlit_error_ctx_handler
@di_inject
async def on_message(  # noqa: PLR0913 — зависимости обработчика
    msg: cl.Message,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    config: Annotated[AgentConfig, Depends(agent_config)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
):
    """Ход по новому вопросу вкладки."""
    tab = TabAgent(agents, registry)
    identity = tab.identity(selected)
    question = tab.question(msg, identity, config.public_url)

    await tab.run(identity, question, TabSettings(app_config, registry, table))


@chainlit_error_ctx_handler
@di_inject
async def on_edit(  # noqa: PLR0913 — зависимости обработчика
    question: Question,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
):
    """Ход по правленому вопросу вкладки: тот же путь, что у нового вопроса;
    вложения правленого вопроса агент вкладки берёт из своей беседы."""
    tab = TabAgent(agents, registry)
    identity = tab.identity(selected)

    await tab.run(identity, question, TabSettings(app_config, registry, table))


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


class SettingsRefresh:
    """Чужая вкладка пользователя после сохранения настроек: если она на том же
    профиле — панель заново, со свежим слоем пользователя. Агент её треда не
    перезапускается: слой чата у каждого треда свой.

    Ставит его ChatCallbacks.install в комнату пользователя (UserRoom); зависимости
    берёт из корневого контейнера, агента вкладки — из её контейнера сессии.
    """

    async def apply(self, socket: WebsocketSession, profile: str) -> None:
        root = Container.require_root("settings refresh")
        settings = TabSettings(
            root.resolved(get_app_config),
            root.resolved(chat_profiles_registry),
            root.resolved(agent_settings_table),
        )
        token = context_var.set(ChainlitContext(socket, ChainlitEmitter(socket)))
        try:
            if settings.selected().name != profile:
                return

            await settings.send(self._tab_agent(current_session()))
        finally:
            context_var.reset(token)

    @staticmethod
    def _tab_agent(session: ChainlitSession) -> ThreadAgent | None:
        """Агент треда вкладки; None — у вкладки нет треда или агента."""
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

    profile = session.chat_profile
    if not profile:
        profile = "none"

    language = session.language
    if not language:
        language = "browser"

    logger.info(
        "chat start: session=%s, thread=%s, profile=%s, language=%s",
        session.id,
        session.thread_id,
        profile,
        language,
    )

    # вкладка присоединилась к треду: рендерер этого инстанса подписан на его область,
    # и ход, начатый на другом инстансе, рисуется здесь так же, как свой
    if thread_id := session.thread_id:
        ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

    if user_id := session.user_id:
        UserRoom.join(UUID(user_id))

    # профиль без разрешённых настроек панель не показывает
    await TabSettings(app_config, registry, table).send(None)

    # процесс агента поднимается вместе с вкладкой: первый вопрос его не ждёт
    if session.thread_id:
        await TabAgent(agents, registry).start(session)


@chainlit_error_ctx_handler
@di_inject
async def on_settings_update(  # noqa: PLR0913 — зависимости обработчика
    settings: dict[str, Any],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    changes: Annotated[AgentSettingsChanges, Depends(agent_settings_changes)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
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

    panel_of = TabSettings(app_config, registry, table)
    tab = TabAgent(agents, registry)
    selected = panel_of.selected()
    identity = tab.identity(selected)
    agent = agents.of_thread(identity.thread_id)
    panel = await panel_of.panel(agent)
    if panel is None:
        return

    try:
        outcome = await changes.apply(identity, panel.parse(settings), agent)
    except TabAgent.FAILURES as exc:
        logger.error(
            "thread %s: settings of profile %s are not applied: %s",
            identity.thread_id,
            selected.name,
            exc,
        )
        await tab.failed(str(exc))
        return

    if outcome is SettingsOutcome.DEFERRED:
        await cl.Message(
            content=TabSettings.NOTICE_DEFERRED, author=TabAgent.AUTHOR
        ).send()

    # панель показывает сохранённое; после перезапуска — с настройками сессии
    await panel_of.send(agent)

    # остальные вкладки пользователя узнают о настройках из его области
    changed = ChatSettingsChanged(profile=selected.name, by_session=session.id)
    await bus.publish(Scope.user(UUID(user_id)), changed, LockToken.local())

    logger.info(
        "settings saved: thread=%s, profile=%s, outcome=%s",
        identity.thread_id,
        selected.name,
        outcome.value,
    )


@cl.on_logout
@di_inject
def on_logout(
    request: Request,
    response: Response,
    config: Annotated[RuntimeConfig, Depends(runtime.get_runtime_config)],
):
    # только свои: на домене живут и чужие приложения, а среди присланных
    # кук попадаются имена, которых http.cookies не принимает ('Path')
    session = config.session
    spec = CookieSpec(
        name=session.cookie,
        samesite=session.cookie_samesite,
        ttl_sec=session.session_ttl_sec,
    )
    SessionCookie(spec).clear(response, request.cookies)


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


@cl.action_callback(CanvasAction.OPEN)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_open(
    action: cl.Action,
    canvas: Annotated[CanvasOpener, Depends(canvas_opener)],
) -> None:
    """Клик по файлу в списке или по ссылке в переписке открывает панель без
    участия агента."""
    scope = current_session().canvas_scope()
    if scope is None:
        logger.warning("canvas open without a thread session: %s", action.payload)
        return

    path = action.payload.get(CanvasAction.PATH.value)
    if not path:
        logger.warning("canvas action without path: %s", action.payload)
        return

    mime = str(action.payload.get(CanvasAction.MIME.value, ""))

    try:
        await canvas.show(str(path), mime, scope)
    except RefusalError as exc:
        await ChatNotices.error(f"Failed to open the canvas: {exc}")


@cl.action_callback(CanvasAction.CONTENT)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_content(
    action: cl.Action,
    canvas: Annotated[CanvasOpener, Depends(canvas_opener)],
) -> dict[str, Any]:
    """Панель уже открыта: отдаём описание файла, не подменяя элемент.

    Панель здесь не трогается — иначе chainlit пересоздал бы её и проиграл
    анимацию открытия заново. refresh — перечитывание уже показанного файла
    по сигналу слежения: слежение не перерегистрируется.
    """
    scope = current_session().canvas_scope()
    if scope is None:
        return {}

    path = action.payload.get(CanvasAction.PATH.value)
    if not path:
        logger.warning("canvas content action without path: %s", action.payload)
        return {}

    refresh = bool(action.payload.get("refresh"))
    mime = str(action.payload.get(CanvasAction.MIME.value, ""))

    try:
        described = await canvas.content(
            str(path), mime, watch=not refresh, scope=scope
        )
    except RefusalError as exc:
        await ChatNotices.error(f"Failed to open the canvas: {exc}")
        return {}

    return described.props()


@cl.action_callback(CanvasAction.SHOW)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_stream(
    action: cl.Action,
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    media: Annotated[ToolMedia, Depends(tool_media)],
) -> dict[str, Any]:
    """Кнопка на шаге инструмента: журнал вызова в панель плюс слежение.

    Открытая панель просит содержимое ответом (inline) — тогда элемент не
    пушится и панель не переоткрывается.

    Пользователь и тред берутся из сессии: чужой журнал по payload недостижим.
    """
    session = current_session()
    scope = session.canvas_scope()
    if scope is None:
        return {}

    logger.info(
        "stream show: user=%s thread=%s payload=%s",
        scope.user_id,
        scope.thread_id,
        dict(action.payload),
    )
    caller = McpCaller(login=session.identifier, roles=session.roles)
    streams = RemoteStreams(journals, servers, caller, media)

    return await StreamActions.show(
        scope.user_id, scope.thread_id, action.payload, streams
    )


@cl.action_callback(CanvasAction.WINDOW)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_stream_window(
    action: cl.Action,
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    media: Annotated[ToolMedia, Depends(tool_media)],
) -> dict[str, Any]:
    """Окно журнала или файла по смещению: панель не подменяется."""
    session = current_session()
    scope = session.canvas_scope()
    if scope is None:
        return {}

    caller = McpCaller(login=session.identifier, roles=session.roles)
    streams = RemoteStreams(journals, servers, caller, media)

    return await StreamActions.window(
        scope.user_id, scope.thread_id, action.payload, streams
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


@cl.action_callback(AppAction.CALL)
@chainlit_error_ctx_handler
@di_inject
async def on_app_call(
    action: cl.Action,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    relay: Annotated[McpRelay, Depends(mcp_relay)],
) -> dict[str, Any]:
    """tools/call приложения MCP Apps у шага: от имени пользователя вкладки в
    её треде."""
    request = AppRequest[mt.CallToolRequestParams].model_validate(action.payload)
    identity = ThreadIdentities().of(current_session(), selected)
    registration = identity.registration(request.server)

    return await relay.app_answer(relay.app_call(registration, request.params))


@cl.action_callback(AppAction.READ)
@chainlit_error_ctx_handler
@di_inject
async def on_app_read(
    action: cl.Action,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    relay: Annotated[McpRelay, Depends(mcp_relay)],
) -> dict[str, Any]:
    """resources/read приложения MCP Apps: и HTML самого приложения, и
    ресурсы, которые оно просит."""
    request = AppRequest[mt.ReadResourceRequestParams].model_validate(action.payload)
    identity = ThreadIdentities().of(current_session(), selected)
    registration = identity.registration(request.server)

    return await relay.app_answer(relay.app_read(registration, request.params))


@cl.action_callback(AppAction.CONTEXT)
@chainlit_error_ctx_handler
@di_inject
async def on_app_context(
    action: cl.Action,
    relay: Annotated[McpRelay, Depends(mcp_relay)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
) -> dict[str, Any]:
    """ui/update-model-context приложения: контекст уйдёт модели со
    следующим вопросом вкладки."""
    request = AppRequest[ModelContextUpdate].model_validate(action.payload)

    agent: ThreadAgent | None = None
    if thread_id := current_session().thread_id:
        agent = agents.of_thread(thread_id)

    if agent is None:
        message = (
            f"ui/update-model-context of the app of call {request.call_id!r}: the "
            "tab has no agent to take the context, ask a question first"
        )
        error = mt.ErrorData(code=mt.INVALID_REQUEST, message=message)
        return relay.app_refusal(error)

    agent.app_context(request.call_id, list(request.params.blocks()))

    return {}


@cl.on_chat_resume
@chainlit_error_ctx_handler
@di_inject
async def on_chat_resume(  # noqa: PLR0913 — зависимости обработчика
    thread_dict: ThreadDict,
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    table: Annotated[AgentSettingsTable, Depends(agent_settings_table)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
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

    tab = TabAgent(agents, registry)
    settings = TabSettings(app_config, registry, table)
    await ThreadResume(tab, settings, bus).enter(thread_dict[ThreadField.ID])


class ThreadResume:
    """Возврат вкладки к треду: комнаты, агент вкладки и воспроизведение
    истории `session/resume` после того, как chainlit отдал вкладке тред (шагов
    в нём нет: их рисует воспроизведение), затем панель настроек с
    настройками сессии. Ход треда, идущий где-то ещё, вкладка догоняет.

    Создаёт его обработчик on_chat_resume на один возврат.
    """

    def __init__(self, tab: TabAgent, settings: TabSettings, bus: MessageBus) -> None:
        self._tab = tab
        self._settings = settings
        self._bus = bus

    async def enter(self, thread_id: str) -> None:
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

        socket = ThreadRoom.websocket()
        agent = await self._tab.start(current_session())

        async def after_resume() -> None:
            if agent is not None:
                await self._tab.load(agent)

            await self._follow_live_turn(socket, thread_id)
            await self._settings.send(agent)

        ResumeEmitter(socket, after_resume).install()

    async def _follow_live_turn(self, socket: WebsocketSession, thread_id: str) -> None:
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

        caught = await renderer.catch_up(self._bus)
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

    def install(self) -> None:
        """Подменяет эмиттер в контексте текущего подключения: chainlit держит
        этот объект контекста локально и зовёт его resume_thread сразу после
        обработчика, поэтому меняется эмиттер объекта, а не сам контекст."""
        context_var.get().emitter = self


class ChatCallbacks:
    """Обработчики чата, которые chainlit и сокет берут не декоратором:
    вопрос, правка вопроса, правка настроек и обновление чужих вкладок
    пользователя. Ставит их bootstrap один раз на процесс; остальные
    обработчики модуля регистрируются декораторами chainlit при импорте.
    """

    def install(self) -> None:
        chainlit_config.code.on_message = wrap_user_function(on_message)
        # без task-обёртки chainlit: её task_end погасил бы индикатор идущего хода
        chainlit_config.code.on_settings_update = wrap_user_function(on_settings_update)
        SocketEvents.on_edit(on_edit)
        UserRoom.on_settings(SettingsRefresh().apply)
