"""Callback'и chainlit: мост между интерфейсом чата и агентом треда по ACP.

Ход — `session/prompt` агента вкладки, лента рисуется из его уведомлений через
шину; вопрос-команду «/» сначала получает реестр команд вкладки и отдаёт её
владельцу; возврат к треду — `session/resume` после того, как chainlit отдал
вкладке сам тред; приложения MCP Apps у шагов вызова зовут свой сервер
действиями mcp_app_*.
"""

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
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
from chainlit.context import context, context_var
from chainlit.data.base import BaseDataLayer
from chainlit.emitter import ChainlitEmitter
from chainlit.session import WebsocketSession
from chainlit.types import ThreadDict
from chainlit.utils import wrap_user_function
from fastapi import Request, Response

from boba.aid.agent.chat import (
    ModelContextUpdate,
    Question,
    Questions,
    ThreadAgent,
    ThreadAgents,
    ThreadAgentsIndex,
    ThreadIdentities,
    ThreadIdentity,
)
from boba.aid.agent.client import AcpClientError
from boba.aid.agent.commands import CommandError
from boba.aid.agent.relay import AppAction, AppRequest, McpRelay
from boba.aid.agent.settings import AgentSettingsError
from boba.aid.auth.chainlit_user import SessionUser
from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.http import SessionCookie
from boba.aid.canvas.panel import PanelStorage, StreamActions
from boba.aid.canvas.remote import RemoteJournals, RemoteStreams
from boba.aid.canvas.tools import CanvasOpener
from boba.aid.chat.ui_text import UiText
from boba.aid.domain.fields import ThreadField, ThreadMetaField
from boba.aid.infra import services
from boba.aid.infra.config import AppConfig
from boba.aid.infra.di import Depends, di_inject
from boba.aid.infra.notices import FailureText, TabAction, TabEvent, TabNotices
from boba.aid.infra.providers import (
    McpUpdates,
    canvas_opener,
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    mcp_browser_oauth,
    mcp_directory,
    mcp_forms,
    mcp_relay,
    mcp_servers,
    mcp_updates,
    remote_journals,
    server_source,
    session_profile,
    thread_agents,
    thread_agents_index,
)
from boba.aid.infra.session import (
    ChainlitSession,
    SessionSignInError,
    current_session,
)
from boba.aid.infra.socket_events import SocketEvents
from boba.aid.infra.thread_room import (
    ChatRoomSurface,
    ThreadLive,
    ThreadRoom,
    UserRoom,
)
from boba.aid.mcp_servers import McpAction
from boba.aid.mcp_servers.directory import (
    McpAppError,
    McpDirectory,
    McpServerDetail,
    McpServerList,
    McpServerRequest,
    McpServerStatus,
    McpViewer,
)
from boba.aid.mcp_servers.form import (
    McpForms,
    McpServerAdd,
    McpServerFormView,
    McpServerToggle,
    McpServerUpdate,
)
from boba.aid.mcp_servers.oauth import McpBrowserOAuth
from boba.aid.mcp_servers.sources import ServerSource
from boba.aid.rendering.errors import chainlit_error_ctx_handler
from boba.cancellation import StopReason
from boba.canvas.canvas import CanvasAction, CanvasFileRequest, RenderVerdicts
from boba.chat.messages import (
    StopRequested,
)
from boba.chat.profiles import ChatProfiles, SelectedProfile
from boba.chat.threads import DataLayerError
from boba.identity.authentication import AuthenticatedPrincipal
from boba.identity.context import Scope
from boba.identity.errors import RefusalError
from boba.identity.locks import LockBusyError
from boba.identity.run import Runs
from boba.identity.session import Login
from boba.mcp_client.client import (
    BrowserOAuthAuth,
    FilesHost,
    HttpAddress,
    McpCaller,
    McpClientError,
    McpServers,
)
from boba.messaging import (
    MessageBus,
)

logger = logging.getLogger(__name__)


class TabText(StrEnum):
    """Ключи пометок вкладке в переводах chainlit: строки — на языке вкладки."""

    PROFILE_RETIRED = "aid.notice.profileRetired"
    """Профиль треда больше не выдан: тред продолжается профилем по умолчанию."""

    def of(self, app_config: AppConfig, **values: object) -> str:
        texts = UiText(Path(chainlit_config.root), current_session().language)
        return texts.text(self.value, **values)


class TabAgent:
    """Агент треда вкладки в обработчиках chainlit: поднять под тред,
    загрузить историю, провести ход, поднять заново по кнопке повтора.
    Отказы пути агента (FAILURES) уходят вкладке через TabNotices: агент
    недоступен — полосой над полем ввода, прочее — всплывающим уведомлением;
    в ленту они не идут — в истории агента их нет.

    Создают обработчики из агентов сессии и профилей на один вызов; вопрос и
    личность вкладки собирают фабрики в его полях.
    """

    FAILURES: ClassVar[tuple[type[Exception], ...]] = (
        AcpClientError,
        AgentSettingsError,
        McpAppError,
        McpClientError,
        LockBusyError,
        CommandError,
    )
    """Отказы пути агента, которые уходят вкладке через TabNotices."""

    def __init__(self, agents: ThreadAgents, registry: ChatProfiles) -> None:
        self._agents = agents
        self._registry = registry
        self._identities = ThreadIdentities()
        self._questions = Questions()
        self._notices = TabNotices()

    def identity(self, profile: SelectedProfile) -> ThreadIdentity:
        """Личность вкладки текущей сессии с профилем profile."""
        return self._identities.of(current_session(), profile)

    def question(
        self, msg: cl.Message, identity: ThreadIdentity, public_url: str
    ) -> Question:
        return self._questions.of_message(msg, identity, public_url)

    async def start(self, session: ChainlitSession) -> ThreadAgent | None:
        """Процесс агента под тред вкладки; без выбранного профиля ждём первого
        хода; None — агент не поднялся, причина — на полосе вкладки."""
        try:
            selected = self._registry.resolve(session.chat_profile, session.access)
        except RefusalError as exc:
            logger.info(
                "thread %s: agent waits for a profile: %s", session.thread_id, exc
            )
            return None

        identity = self._identities.of(session, selected)
        try:
            agent = await self._agents.of(identity)
        except self.FAILURES as exc:
            logger.error("thread %s: agent start failed: %s", identity.thread_id, exc)
            await self._notices.failed(exc)

            return None

        await self._notices.agent_ready()

        return agent

    async def load(self, agent: ThreadAgent) -> None:
        """Агент вкладки загружает сессию треда с воспроизведением истории в
        ленту; отказ агента — вкладке."""
        try:
            loaded = await agent.resume()
        except self.FAILURES as exc:
            logger.error("thread %s: agent resume failed: %s", agent.thread_id, exc)
            await self._notices.failed(exc)
            return

        if not loaded:
            logger.info(
                "thread %s: no agent session yet, a new one opens on the first turn",
                agent.thread_id,
            )

    async def retry(self, session: ChainlitSession) -> None:
        """Кнопка повтора на полосе недоступного агента: агент треда, который
        вкладка уже держит, поднимается заново с лентой из истории; агент,
        который не поднялся при открытии, — запуском и загрузкой истории.
        Полоса снимается до итога повтора; отказ — снова на ней."""
        await self._notices.agent_ready()

        known = None
        if thread_id := session.thread_id:
            known = self._agents.of_thread(thread_id)

        if known is None:
            agent = await self.start(session)
            if agent is not None:
                await self.load(agent)

            return

        try:
            await known.recover()
        except self.FAILURES as exc:
            logger.error("thread %s: agent recovery failed: %s", known.thread_id, exc)
            await self._notices.failed(exc)
            return

        await self._notices.agent_ready()

    async def run(
        self, identity: ThreadIdentity, question: Question, command: str | None
    ) -> None:
        """Ход по вопросу: вопрос-команду (command — команда, выбранная в
        меню) сначала получает реестр команд вкладки, команду чата или
        приложения сервера исполняет вкладка без хода; остальное — агенту
        вкладки `session/prompt`, лента рисуется из его уведомлений через
        шину. Отказ команды или агента и занятый тред уходят вкладке через
        TabNotices, а при отказе правки вкладка сначала заново рисует беседу
        из истории — фронт уже обрезал её ленту. Полоса недоступного агента —
        состояние связи, а не итог прошлого хода: новое действие снимает её
        до своего итога."""
        await self._notices.agent_ready()

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
            await self._notices.failed(exc)
            return

        try:
            routed = await agent.routed(question, command)
        except self.FAILURES as exc:
            logger.error(
                "thread %s: command of question %s failed: %s",
                thread_id,
                question.key,
                exc,
            )
            await self._notices.failed(exc)
            return

        if routed is None:
            return

        try:
            asked = routed
            if routed.edited:
                asked = agent.edited(routed)

            await agent.ask(asked)
        except self.FAILURES as exc:
            logger.error("thread %s: turn %s failed: %s", thread_id, question.key, exc)
            await self._redrawn(agent, question)
            await self._notices.failed(exc)

    async def _redrawn(self, agent: ThreadAgent, question: Question) -> None:
        """Отказ правки: вкладка заново рисует беседу из истории — фронт уже
        обрезал её ленту, а свой идущий ход не трогается. Не вышло — причина
        вкладке."""
        if not question.edited:
            return

        if agent.turn_active():
            return

        try:
            await agent.reload()
        except self.FAILURES as redraw:
            logger.error(
                "thread %s: redrawing the tab after turn %s failed: %s",
                agent.thread_id,
                question.key,
                redraw,
            )
            await self._notices.failed(redraw)
        except DataLayerError as redraw:
            logger.error(
                "thread %s: redrawing the tab after turn %s failed: %s",
                agent.thread_id,
                question.key,
                redraw,
            )
            await self._notices.toast(FailureText.REDRAW, redraw.view_message().content)


@chainlit_error_ctx_handler
@di_inject
async def on_message(
    msg: cl.Message,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    config: Annotated[AppConfig, Depends(get_app_config)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
):
    """Ход по новому вопросу вкладки."""
    tab = TabAgent(agents, registry)
    identity = tab.identity(selected)
    question = tab.question(
        msg, identity, config.agent.public_url or config.server.public_url
    )

    await tab.run(identity, question, msg.command)


@chainlit_error_ctx_handler
@di_inject
async def on_edit(
    question: Question,
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
):
    """Ход по правленому вопросу вкладки: тот же путь, что у нового вопроса;
    вложения правленого вопроса агент вкладки берёт из своей беседы."""
    tab = TabAgent(agents, registry)
    identity = tab.identity(selected)

    await tab.run(identity, question, None)


@di_inject
async def show_question(
    session: ChainlitSession,
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
) -> None:
    """Вкладка подключилась к треду с идущим ходом: висящий вопрос агента
    этого треда и этого пользователя на инстансе показывается в ней — после
    обрыва сокета и после перезагрузки страницы; вопрос, открытый в другой
    живой вкладке, там и остаётся."""
    thread_id = session.thread_id
    if thread_id is None:
        return

    user_id = session.user_id
    if user_id is None:
        return

    socket = session.websocket
    if socket is None:
        return

    live: set[str] = set()
    for tab in ThreadRoom.sessions(thread_id):
        live.add(tab.id)

    for agent in index.of_thread(thread_id):
        if agent.user_id != UUID(user_id):
            continue

        if await agent.question_shown(socket, live):
            return


@cl.set_chat_profiles
@di_inject
async def set_chat_profiles(
    user: cl.User | None,
    language: str | None,
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    source: Annotated[ServerSource, Depends(server_source)],
) -> list[cl.ChatProfile]:
    """Профили, выданные входу пользователя; выбор профиля обязателен.
    Загрузка файлов включена, если среди выданных пользователю серверов
    есть объявивший хранилище файлов: своего хранилища у чата нет."""
    principal = user.principal if isinstance(user, SessionUser) else None
    if principal is None:
        try:
            principal = current_session().authenticated_user()
        except SessionSignInError:
            principal = None

    granted = principal.profiles if principal is not None else frozenset()
    uploads = ProfileUploads(servers, source, principal)
    overrides = await uploads.overrides()

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
                config_overrides=overrides,
            )
        )

    return profiles


class ProfileUploads:
    """Доступность загрузки файлов для пользователя.

    Создаётся обработчиком set_chat_profiles на один запрос настроек и
    on_chat_start на старт вкладки. Файлы пользователя лежат на первом из
    выданных ему серверов, объявившем расширение файлов; если такого нет или
    сервер недоступен, кнопка загрузки скрыта, а недоступность сервера
    вкладка узнаёт уведомлением (explain).
    """

    def __init__(
        self,
        servers: McpServers,
        source: ServerSource,
        principal: AuthenticatedPrincipal | None,
    ) -> None:
        self._servers = servers
        self._source = source
        self._principal = principal

    async def overrides(self) -> ChainlitConfigOverrides:
        upload = SpontaneousFileUploadFeature(enabled=await self.enabled())

        return ChainlitConfigOverrides(
            features=FeaturesSettings(spontaneous_file_upload=upload)
        )

    async def enabled(self) -> bool:
        try:
            host = await self._host()
        except (McpClientError, McpAppError) as exc:
            logger.warning("file upload is hidden: %s", exc)
            return False

        return host is not None

    async def explain(self, notices: TabNotices) -> None:
        """Кнопка загрузки скрыта недоступным сервером — вкладке уведомление
        с причиной; доступный сервер или отсутствие серверов с файлами
        молчат."""
        try:
            await self._host()
        except McpClientError as exc:
            logger.warning("file upload is hidden: %s", exc)
            await notices.uploads_off(exc)

    async def _host(self) -> FilesHost | None:
        principal = self._principal
        if principal is None:
            return None

        try:
            viewer = self._viewer(principal)
            names = await self._source.enabled_names(viewer)
        except McpAppError as exc:
            raise McpClientError(
                f"Looking up file servers of {principal.identifier!r} failed: {exc}"
            ) from exc

        if not names:
            return None

        return await self._servers.files_host(names, viewer.caller())

    def _viewer(self, principal: AuthenticatedPrincipal) -> McpViewer:
        """Ошибки:
        McpAppError — у входа нет строки users: серверы ему не выданы.
        """
        return McpViewer(
            user_id=principal.id,
            login=Login(principal.identifier),
            roles=principal.roles,
        )


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start(
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    source: Annotated[ServerSource, Depends(server_source)],
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

    # процесс агента поднимается вместе с вкладкой: первый вопрос его не ждёт
    if session.thread_id:
        await TabAgent(agents, registry).start(session)

    # профиль, недоступный ролям входа, вкладке не открылся: про загрузку молчим
    try:
        registry.resolve(session.chat_profile, session.access)
    except RefusalError:
        return

    uploads = ProfileUploads(servers, source, session.authenticated_user())
    await uploads.explain(TabNotices())


@cl.on_chat_end
@chainlit_error_ctx_handler
@di_inject
async def on_chat_end(
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
):
    """Сокет вкладки оборвался: сессия chainlit ждёт реконнекта до
    session_timeout, а процессы агентов её тредов отпускаются сразу —
    вернувшаяся вкладка запустит новые первым же делом."""
    await agents.release()


@cl.on_logout
@di_inject
def on_logout(
    request: Request,
    response: Response,
    config: Annotated[AppConfig, Depends(services.application_config)],
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
@chainlit_error_ctx_handler
@di_inject
async def on_stop(
    bus: Annotated[MessageBus, Depends(services.message_bus)],
    instance: Annotated[str, Depends(services.instance_name)],
    runs: Annotated[Runs, Depends(services.runs)],
    index: Annotated[ThreadAgentsIndex, Depends(thread_agents_index)],
):
    """Кнопка Stop: ход агента любой вкладки этого инстанса получает
    `session/cancel`, чужой ход — команду через шину."""
    session = current_session()
    thread_id = session.thread_id
    if thread_id is None:
        return

    if agent := index.active(thread_id):
        try:
            await agent.stop()
        except AcpClientError as exc:
            logger.error("thread %s: stop failed: %s", thread_id, exc)
            await TabNotices().failed(exc)

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
    request = CanvasFileRequest.model_validate(action.payload)
    scope = await current_session().canvas_scope(PanelStorage.client())
    if scope is None:
        logger.warning("canvas open without a thread session: %s", action.payload)
        return

    try:
        await canvas.show(request.path, request.mime, scope)
    except RefusalError as exc:
        await TabNotices().canvas_refused(exc)


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
    request = CanvasFileRequest.model_validate(action.payload)
    scope = await current_session().canvas_scope(PanelStorage.client())
    if scope is None:
        return {}

    try:
        described = await canvas.content(
            request.path, request.mime, watch=not request.refresh, scope=scope
        )
    except RefusalError as exc:
        await TabNotices().canvas_refused(exc)
        return {}

    return described.props()


@cl.action_callback(CanvasAction.SHOW)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_stream(
    action: cl.Action,
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
) -> dict[str, Any]:
    """Кнопка на шаге инструмента: журнал вызова в панель плюс слежение.

    Открытая панель просит содержимое ответом (inline) — тогда элемент не
    пушится и панель не переоткрывается.

    Пользователь и тред берутся из сессии: чужой журнал по payload недостижим.
    """
    session = current_session()
    owner = session.canvas_owner()
    if owner is None:
        return {}

    user_id, thread_id = owner

    logger.info(
        "stream show: user=%s thread=%s payload=%s",
        user_id,
        thread_id,
        dict(action.payload),
    )
    caller = McpCaller(login=session.identifier, roles=session.roles)
    streams = RemoteStreams(journals, servers, caller)

    return await StreamActions.show(user_id, thread_id, action.payload, streams)


@cl.action_callback(CanvasAction.WINDOW)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_stream_window(
    action: cl.Action,
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
) -> dict[str, Any]:
    """Окно журнала или файла по смещению: панель не подменяется."""
    session = current_session()
    owner = session.canvas_owner()
    if owner is None:
        return {}

    user_id, thread_id = owner

    caller = McpCaller(login=session.identifier, roles=session.roles)
    streams = RemoteStreams(journals, servers, caller)

    return await StreamActions.window(user_id, thread_id, action.payload, streams)


@cl.action_callback(CanvasAction.HOLD)
@chainlit_error_ctx_handler
@di_inject
async def on_canvas_hold(
    action: cl.Action,
    journals: Annotated[RemoteJournals, Depends(remote_journals)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
) -> None:
    """Панель держит свой показ — при монтировании и после переподключения
    сокета вкладки: слежение показа, которого уже нет, ставится заново."""
    session = current_session()
    owner = session.canvas_owner()
    if owner is None:
        return

    user_id, thread_id = owner
    caller = McpCaller(login=session.identifier, roles=session.roles)
    streams = RemoteStreams(journals, servers, caller)

    await StreamActions.hold(user_id, thread_id, action.payload, streams)


@cl.action_callback(CanvasAction.LEAVE)
@chainlit_error_ctx_handler
async def on_canvas_leave(action: cl.Action) -> None:
    """Экземпляр панели закрыт или сменил файл: слежение прежнего показа
    снимается, когда его не держит никто."""
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


@cl.action_callback(TabAction.RETRY_AGENT)
@chainlit_error_ctx_handler
@di_inject
async def on_agent_retry(
    action: cl.Action,
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
) -> None:
    """Кнопка повтора на полосе недоступного агента вкладки."""
    await TabAgent(agents, registry).retry(current_session())


class McpViewerFactory:
    """Сборка зрителя приложения MCP-серверов из сессии вкладки и её профиля."""

    def __init__(self, session: ChainlitSession) -> None:
        self._session = session

    def viewer(self) -> McpViewer:
        """Ошибки:
        McpAppError — у сессии нет строки users: свой сервер заводить не на кого.
        """
        user_id = self._session.user_id
        if not user_id:
            msg = (
                f"the MCP servers app of session {self._session.id!r} was not "
                f"opened: the sign-in {self._session.label!r} has no users row"
            )
            raise McpAppError(msg)

        try:
            return McpViewer(
                user_id=UUID(user_id),
                login=self._session.identifier,
                roles=self._session.roles,
            )
        except ValueError:
            raise McpAppError(
                "Opening MCP panel: the signed-in session has no valid user id or login"
            ) from None


def _mcp_answer(
    answer: McpServerList | McpServerStatus | McpServerDetail | McpServerFormView,
) -> dict[str, Any]:
    return answer.model_dump(mode="json")


def _mcp_app_error(exc: Exception) -> dict[str, Any]:
    """Ответ с ошибкой приложения: фронт показывает её строкой."""
    if isinstance(exc, McpAppError):
        return {"error": str(exc)}

    msg = f"the MCP servers app did not answer: {exc}"
    raise RuntimeError(msg) from exc


@cl.action_callback(McpAction.LIST)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_list(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Список серверов профиля зрителя."""
    try:
        return _mcp_answer(
            await directory.listed(McpViewerFactory(current_session()).viewer())
        )
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.STATUS)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_status(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Состояние одного сервера приложения."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        return _mcp_answer(
            await directory.status(
                McpViewerFactory(current_session()).viewer(), request.name
            )
        )
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.DETAIL)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_detail(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Карточка сервера приложения с инструментами."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        return _mcp_answer(
            await directory.detail(
                McpViewerFactory(current_session()).viewer(), request.name
            )
        )
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.RECONNECT)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_reconnect(
    action: cl.Action,
    updates: Annotated[McpUpdates, Depends(mcp_updates)],
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Кнопка Reconnect карточки: переподключение и новое состояние."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        status = await directory.reconnect(viewer, request.name)
        listed = await directory.listed(viewer)
        for row in listed.servers:
            if row.name != request.name:
                continue

            if row.editable:
                await updates.changed(viewer.user_id, (request.name,))

        return _mcp_answer(status)
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.ADD)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_add(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
    updates: Annotated[McpUpdates, Depends(mcp_updates)],
) -> dict[str, Any]:
    """Действие add панели MCP-серверов текущего пользователя."""
    try:
        request = forms.parse(McpServerAdd, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        answer = await directory.save(viewer, request.form)
        await updates.changed(viewer.user_id, (request.form.name,))

        return _mcp_answer(answer)
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.UPDATE)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_update(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
    updates: Annotated[McpUpdates, Depends(mcp_updates)],
) -> dict[str, Any]:
    """Действие update панели MCP-серверов текущего пользователя."""
    try:
        request = forms.parse(McpServerUpdate, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        answer = await directory.save(viewer, request.form, request.current)
        await updates.changed(viewer.user_id, (request.current, request.form.name))

        return _mcp_answer(answer)
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.REMOVE)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_remove(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
    updates: Annotated[McpUpdates, Depends(mcp_updates)],
) -> dict[str, Any]:
    """Действие remove панели MCP-серверов текущего пользователя."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        answer = await directory.remove(viewer, request.name)
        await updates.changed(viewer.user_id, (request.name,))

        return _mcp_answer(answer)
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.TOGGLE)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_toggle(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
    updates: Annotated[McpUpdates, Depends(mcp_updates)],
) -> dict[str, Any]:
    """Действие toggle панели MCP-серверов текущего пользователя."""
    try:
        request = forms.parse(McpServerToggle, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        answer = await directory.set_enabled(viewer, request.name, request.enabled)
        await updates.changed(viewer.user_id, (request.name,))

        return _mcp_answer(answer)
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.OAUTH_START)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_oauth_start(
    action: cl.Action,
    source: Annotated[ServerSource, Depends(server_source)],
    oauth: Annotated[McpBrowserOAuth, Depends(mcp_browser_oauth)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Begin authorization only for an OAuth server granted to this user."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        record = await source.record(viewer, request.name)
        endpoint = record.config.endpoint
        if not record.enabled or not isinstance(endpoint, HttpAddress):
            raise McpAppError("MCP OAuth server must be an enabled remote server")
        if not isinstance(endpoint.auth, BrowserOAuthAuth):
            raise McpAppError("MCP server does not use browser OAuth")
        url = await oauth.begin(
            record.server_id, viewer.user_id, viewer.login, endpoint.url()
        )
        return {"url": url}
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.OAUTH_DISCONNECT)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_oauth_disconnect(
    action: cl.Action,
    source: Annotated[ServerSource, Depends(server_source)],
    oauth: Annotated[McpBrowserOAuth, Depends(mcp_browser_oauth)],
    servers: Annotated[McpServers, Depends(mcp_servers)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Remove only this user's OAuth credentials, including role servers."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        record = await source.record(viewer, request.name)
        endpoint = record.config.endpoint
        if not isinstance(endpoint, HttpAddress):
            raise McpAppError("MCP server does not use browser OAuth")
        if not isinstance(endpoint.auth, BrowserOAuthAuth):
            raise McpAppError("MCP server does not use browser OAuth")
        await oauth.disconnect(record.server_id, viewer.user_id)
        await servers.reconnect(record.name, viewer.caller())
        return {}
    except McpClientError as exc:
        return _mcp_app_error(McpAppError(str(exc)))
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.action_callback(McpAction.FORM)
@chainlit_error_ctx_handler
@di_inject
async def on_mcp_form(
    action: cl.Action,
    directory: Annotated[McpDirectory, Depends(mcp_directory)],
    forms: Annotated[McpForms, Depends(mcp_forms)],
) -> dict[str, Any]:
    """Действие form панели MCP-серверов текущего пользователя."""
    try:
        request = forms.parse(McpServerRequest, action.payload)
        viewer = McpViewerFactory(current_session()).viewer()
        answer = await directory.form(viewer, request.name)
        return _mcp_answer(answer)
    except McpAppError as exc:
        return _mcp_app_error(exc)


@cl.on_chat_resume
@chainlit_error_ctx_handler
@di_inject
async def on_chat_resume(
    thread_dict: ThreadDict,
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    agents: Annotated[ThreadAgents, Depends(thread_agents, scope="session")],
    bus: Annotated[MessageBus, Depends(services.message_bus)],
):
    """Вкладка вернулась к треду.

    Профиль из meta треда, которого нет в конфиге или который не выдан входу,
    заменяется профилем по умолчанию до сборки агента сессии: иначе агент не
    собирается, а фронт без конца переключает профиль. Этот же thread_dict
    chainlit следом отправит фронту, а профиль сессии запишет в meta на
    disconnect.
    """
    session = current_session()
    sign_in = session.access

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
        notice = TabText.PROFILE_RETIRED.of(
            app_config, retired=stored, profile=fallback.config.display_name
        )
        await context.emitter.send_toast(notice, "warning")

    tab = TabAgent(agents, registry)
    await ThreadResume(tab, bus).enter(thread_dict[ThreadField.ID])


class ThreadResume:
    """Возврат вкладки к треду: комнаты, агент вкладки и воспроизведение
    истории `session/resume` после того, как chainlit отдал вкладке тред (шагов
    в нём нет: их рисует воспроизведение). Ход треда, идущий где-то ещё,
    вкладка догоняет. Агент поднимается тоже после отдачи треда, вне задачи
    chainlit вокруг on_chat_resume: иначе вкладка всё это время видит «идёт
    ход» (кнопку Stop) вместо загрузки. Конец загрузки вкладка узнаёт событием
    TabEvent.HISTORY_LOADED при любом исходе.

    Создаёт его обработчик on_chat_resume на один возврат.
    """

    def __init__(self, tab: TabAgent, bus: MessageBus) -> None:
        self._tab = tab
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
        current = current_session()

        async def after_resume() -> None:
            try:
                agent = await self._tab.start(current)
                if agent is not None:
                    await self._tab.load(agent)

                await self._follow_live_turn(socket, thread_id)
            finally:
                await current.emit(TabEvent.HISTORY_LOADED.value, {})

            logger.info("resume thread %s: session %s is ready", thread_id, socket.id)

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
    (resume_thread) исполняет отложенное — воспроизведение истории и
    догон идущего хода.
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
    вопрос, правка вопроса и висящий вопрос агента вкладке, подключившейся к
    треду. Ставит
    их bootstrap один раз на процесс; остальные обработчики модуля
    регистрируются декораторами chainlit при импорте.
    """

    def install(self) -> None:
        chainlit_config.code.on_message = wrap_user_function(on_message)
        SocketEvents.on_edit(on_edit)
        SocketEvents.on_ready(show_question)
