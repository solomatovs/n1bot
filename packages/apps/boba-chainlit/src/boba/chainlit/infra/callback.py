"""Callback'и chainlit: мост между интерфейсом чата и агентом сессии."""

import logging
from collections.abc import Sequence
from typing import Annotated, Any
from uuid import UUID

from fastapi import Request, Response

import chainlit as cl
from boba.cancellation import StopReason
from boba.canvas.canvas import CanvasAction, RenderVerdicts
from boba.chainlit.canvas.panel import StreamActions
from boba.chainlit.canvas.remote import RemoteStreams
from boba.chainlit.canvas.tools import CanvasActions, CanvasScope
from boba.chainlit.chat.dialog import Attachment, SessionAgent
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.history import InterruptedTurn, ThreadRewind, ThreadTurnHistory
from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.chat.settings import SettingsPanel
from boba.chainlit.chat.tracing import LlmStateLog
from boba.chainlit.chat.turn import ChatTurn, Question
from boba.chainlit.data.data_layer import AttachmentDataLayer, PostgresDataLayer
from boba.chainlit.domain.fields import ThreadField, ThreadMetaField
from boba.chainlit.infra import providers
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.providers import (
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    langchain_agent,
    mcp_servers,
    session_profile,
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
from boba.identity.context import CallContexts, Scope
from boba.identity.errors import InternalServiceError
from boba.identity.locks import LiveLocks, RunLocking
from boba.identity.run import Runs
from boba.identity.session import Login, UserMetadataField
from boba.identity.token import CookieSpec
from boba.mcp_client.client import McpCaller, McpServers
from boba.messaging import (
    ChatSettingsChanged,
    LockToken,
    MessageBus,
    PayloadStore,
    StopRequested,
    ThreadRewound,
)
from boba.runtime import providers as runtime
from boba.runtime.config import RuntimeConfig
from boba.runtime.di import Container, Depends, di_inject
from boba.runtime.http import SessionCookie
from boba.transport.http import DumpLabel
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


@chainlit_error_ctx_handler
@di_inject
async def on_message(  # noqa: PLR0913
    msg: cl.Message,
    agent: Annotated[SessionAgent, Depends(langchain_agent, scope="session")],
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    payloads: Annotated[PayloadStore, Depends(runtime.payload_store)],
    locks: Annotated[LiveLocks, Depends(runtime.live_locks)],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    contexts: Annotated[CallContexts, Depends(runtime.call_contexts)],
    runs: Annotated[Runs, Depends(runtime.runs)],
):
    session = current_session()
    thread_id = session.thread_id
    if thread_id is None:
        raise InternalServiceError(
            internal_detail=(
                f"on_message: chainlit session {session.id!r} has no thread_id"
            ),
            user_detail=None,
        )

    ThreadRoom.activate(thread_id)

    # рендерер треда подписан на область до первого сообщения хода
    ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

    feed = TurnFeed(bus, payloads, Scope.chat(thread_id), msg.id, LockToken.local())
    turn = ChatTurn(
        thread_id=thread_id,
        feed=feed,
        history=ThreadTurnHistory(agent, thread_id),
        question=Question.of_message(
            msg, thread_id, AttachmentDataLayer.require().links
        ),
        locking=RunLocking(locks=locks, heartbeat_sec=app_config.cluster.heartbeat_sec),
        contexts=contexts,
        runs=runs,
    )

    # сбой в любом месте хода — включая подготовку — отчитывается ходом же:
    # чат, история и журнал получают одну формулировку
    try:
        # профиль — тот, по которому собран агент сессии, а не сырой выбор вкладки
        context = session.call_context(msg.id, selected.name)
    except Exception as e:
        await turn.crash(e)
        return

    with contexts.applied(context):
        # дампы HTTP-обмена хода именуются пользователем и тредом
        who = session.label
        if not who:
            who = "anon"

        DumpLabel.set(f"{who}-{thread_id}")

        try:
            rewind = ThreadRewind(agent, data_layer, thread_id)
            carried: Sequence[Attachment] = ()
            if await rewind.is_edit(msg.id):
                plan = await rewind.apply(msg.id, msg.content)
                carried = plan.attachments
                rewound = ThreadRewound(turn_id=msg.id)
                await bus.publish(Scope.chat(thread_id), rewound, LockToken.local())

            state_log = LlmStateLog(context.log_mark())
            asked = ChatTurn.user_message(msg, context.subject.user_key, carried)
            stream = agent.answer(
                thread_id, asked, [turn.tracer, state_log], turn.tracer
            )

            await turn.run(stream)
        except Exception as e:
            await turn.crash(e)


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
    return SettingsView.of(config.settings, selected.config, saved)


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


async def _reset_session_container() -> None:
    """Закрывает DI-контейнер сессии: следующий ход соберёт агента заново."""
    await SessionContainers.close(current_session().id)


class SettingsRefresh:
    """Чужая вкладка пользователя после сохранения настроек: если она на том же
    профиле — свежие metadata в сессию, агент заново и панель заново.
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
        token = context_var.set(ChainlitContext(socket, ChainlitEmitter(socket)))
        try:
            session = current_session()
            selected = registry.resolve(session.chat_profile, session.sign_in)
            if selected.name != profile:
                return

            await SettingsRefresh._adopt_metadata(
                session, root.resolved(chainlit_data_layer)
            )
            await _reset_session_container()
            if tabs := _session_settings(app_config, registry):
                await cl.ChatSettings(tabs).send()
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


def _session_settings(app_config: AppConfig, registry: ChatProfiles) -> list[Tab]:
    """Вкладки панели настроек сессии; пусто — профиль ничего не открывает."""
    panel = SettingsPanel(
        _session_view(app_config, registry),
        PanelText(app_config.chainlit.root, current_session().language),
    )
    return panel.tabs()


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
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
    if tabs := _session_settings(app_config, registry):
        await cl.ChatSettings(tabs).send()


@cl.on_settings_update
@chainlit_error_ctx_handler
@di_inject
async def on_settings_update(
    settings: dict[str, Any],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
):
    """Сохраняет настройки пользователя и пересобирает агента сессии."""
    selected = _session_selected_profile(registry)

    panel = SettingsPanel(
        _session_view(app_config, registry),
        PanelText(app_config.chainlit.root, current_session().language),
    )
    overrides = panel.parse(settings).overrides

    user_id = current_session().user_id
    if user_id is None:
        logger.warning(
            "settings update: chainlit session %r has no user, ignored",
            current_session().id,
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
    await _reset_session_container()

    # остальные вкладки пользователя узнают о настройках из его области
    changed = ChatSettingsChanged(
        profile=selected.name, by_session=current_session().id
    )
    await _root_bus().publish(Scope.user(UUID(user_id)), changed, LockToken.local())

    logger.info(
        "llm settings saved: profile=%s, overrides=%s",
        selected.name,
        sorted(overrides.stored()),
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
):
    """Кнопка Stop: свой ход обрывается сразу, чужой получает команду через шину."""
    session = current_session()
    thread_id = session.thread_id
    if thread_id is None:
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
    agent: Annotated[SessionAgent, Depends(langchain_agent, scope="session")],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
    runs: Annotated[Runs, Depends(runtime.runs)],
):
    """Лента треда при возврате вкладки: если ход жив — сохранить loading и
    живые шаги.

    task_start уже отправлен обёрткой chainlit вокруг хендлера; её же task_end
    глушится, пока ход не закончится. Незавершённых шагов ещё нет в истории,
    а stream_token дописывает только в существующее сообщение — подкладываем
    их в ленту до её отправки клиенту.
    """
    thread_id = thread_dict[ThreadField.ID]
    turn = runs.active(thread_id)
    renderer = ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)
    if user_id := current_session().user_id:
        UserRoom.join(UUID(user_id))

    # обновление страницы приходит resume'ом: панель настроек шлётся как на старте
    if tabs := _session_settings(app_config, registry):
        await cl.ChatSettings(tabs).send()

    room: list[str] = []
    for session in ThreadRoom.sessions(thread_id):
        room.append(session.id)

    turn_state = "none"
    if turn is not None:
        turn_state = "local"

    logger.info(
        "resume thread %s: turn=%s, thread sessions=%s, current session=%s",
        thread_id,
        turn_state,
        room,
        current_session().id,
    )

    if turn is not None:
        renderer.resume_into(thread_dict)
        ThreadRoom.keep_loading()
        return

    # ход ведёт другой инстанс: рендерер этого процесса догоняет его по шине
    if not await ThreadLive.turn_alive(thread_id):
        return

    caught = await renderer.catch_up(_root_bus())
    logger.info("resume thread %s: foreign turn, caught up: %s", thread_id, caught)
    if caught.interrupted:
        await InterruptedTurn(agent, thread_id).remember(caught.interrupted)

    if not caught.alive:
        return

    renderer.resume_into(thread_dict)
    ThreadRoom.keep_loading()
