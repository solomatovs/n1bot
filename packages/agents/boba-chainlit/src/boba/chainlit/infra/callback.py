"""Callback'и chainlit: мост между интерфейсом чата и агентом langgraph."""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import Request, Response
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.canvas.canvas import CanvasAction, RenderVerdicts
from boba.chainlit.canvas.panel import StreamActions
from boba.chainlit.canvas.tools import CanvasActions, CanvasScope
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.history import GraphTurnHistory, InterruptedTurn, ThreadRewind
from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.chat.settings import SettingsPanel
from boba.chainlit.chat.tracing import LlmStateLog
from boba.chainlit.chat.turn import ChatTurn, Question
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.domain.fields import ThreadField
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.providers import (
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    langchain_agent,
    session_profile,
)
from boba.chainlit.infra.session import ChainlitSession, current_session
from boba.chainlit.infra.thread_room import ChatRoomSurface, ThreadLive, ThreadRoom
from boba.chainlit.rendering.errors import chainlit_error_ctx_handler
from boba.chat.profiles import (
    ChatProfiles,
    SelectedProfile,
    SettingsView,
    UserLlmOverrides,
    UserMeta,
)
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError
from boba.identity.locks import RunLocking
from boba.identity.session import UserMetadataField
from boba.messaging import LockToken, PayloadStore, StopRequested
from boba.runtime import providers as runtime
from boba.runtime.bus import PgMessageBus
from boba.runtime.di import Container, Depends, di_inject
from boba.runtime.locks import PgLiveLocks
from chainlit.auth.cookie import clear_auth_cookie
from chainlit.config import config as chainlit_config
from chainlit.data.base import BaseDataLayer
from chainlit.input_widget import Tab
from chainlit.types import ThreadDict
from chainlit.utils import wrap_user_function

logger = logging.getLogger(__name__)


@chainlit_error_ctx_handler
@di_inject
async def on_message(  # noqa: PLR0913
    msg: cl.Message,
    graph: Annotated[
        CompiledStateGraph,
        Depends(langchain_agent, scope="session"),
    ],
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    bus: Annotated[PgMessageBus, Depends(runtime.message_bus)],
    payloads: Annotated[PayloadStore, Depends(runtime.payload_store)],
    locks: Annotated[PgLiveLocks, Depends(runtime.live_locks)],
    app_config: Annotated[AppConfig, Depends(get_app_config)],
):
    session = current_session()
    thread_id = session.thread_id
    if thread_id is None:
        raise InternalServiceError(
            internal_detail="on_message outside a chainlit thread",
            user_detail=None,
        )

    ThreadRoom.activate(thread_id)

    # рендерер треда подписан на область до первого сообщения хода
    ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

    feed = TurnFeed(bus, payloads, Scope.chat(thread_id), msg.id, LockToken.local())
    turn = ChatTurn(
        thread_id=thread_id,
        feed=feed,
        history=GraphTurnHistory(graph, thread_id),
        question=Question(key=msg.id, text=msg.content),
        locking=RunLocking(locks=locks, heartbeat_sec=app_config.cluster.heartbeat_sec),
    )

    # сбой в любом месте хода — включая подготовку — отчитывается ходом же:
    # чат, история и журнал получают одну формулировку
    try:
        # профиль — тот, по которому собран агент сессии, а не сырой выбор вкладки
        context = session.call_context(msg.id, selected.name)
    except Exception as e:
        await turn.crash(e)
        return

    with context.applied():
        try:
            rewind = ThreadRewind(graph, data_layer, thread_id)
            if await rewind.is_edit(msg.id):
                await rewind.apply(msg.id, msg.content)
                await rewind.refresh_view()

            state_log = LlmStateLog(context.log_mark())
            run_config = RunnableConfig(
                callbacks=[turn.tracer, state_log],
                configurable={"thread_id": thread_id},
            )

            human = ChatTurn.human_message(msg, context.subject.user_key)
            stream = cast(
                "AsyncIterator[tuple[BaseMessage, dict[str, Any]]]",
                graph.astream(
                    {"messages": [human]},
                    stream_mode="messages",
                    config=run_config,
                ),
            )

            await turn.run(stream)
        except Exception as e:
            await turn.crash(e)


chainlit_config.code.on_message = wrap_user_function(on_message)


def _root_bus() -> PgMessageBus:
    """Шина процесса из корневого контейнера для обработчиков без DI-инъекции."""
    root = Container.root
    if root is None:
        raise InternalServiceError(
            internal_detail="DI container is not initialised", user_detail=None
        )

    return root.resolved(runtime.message_bus)


@cl.set_chat_profiles
@di_inject
async def set_chat_profiles(
    user: cl.User | None,
    language: str | None,
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
) -> list[cl.ChatProfile]:
    """Профили, видимые ролям пользователя; выбор профиля обязателен."""
    roles = ChainlitSession.roles_of(user)

    profiles: list[cl.ChatProfile] = []
    for name, profile in registry.visible_for(roles).items():
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
            )
        )

    return profiles


def _session_selected_profile(registry: ChatProfiles) -> SelectedProfile:
    session = current_session()

    return registry.resolve(session.chat_profile, session.roles)


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
    session = current_session()

    container = session.value(Container.SESSION_KEY)
    if isinstance(container, Container):
        await container.aclose()

    session.remember(Container.SESSION_KEY, None)


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
        logger.warning("settings update without a user session, ignored")
        return

    if not isinstance(data_layer, PostgresDataLayer):
        msg = f"data layer is not PostgresDataLayer: {type(data_layer)}"
        raise RuntimeError(msg)

    await data_layer.update_user_llm_settings(
        int(user_id), selected.name, overrides.stored()
    )

    _refresh_session_user_meta(selected.name, overrides)
    await _reset_session_container()

    logger.info(
        "llm settings saved: profile=%s, overrides=%s",
        selected.name,
        sorted(overrides.stored()),
    )


@cl.on_logout
def on_logout(request: Request, response: Response):
    # только свои: на домене живут и чужие приложения, а среди присланных
    # кук попадаются имена, которых http.cookies не принимает ('Path')
    clear_auth_cookie(request, response)


@cl.on_stop
@di_inject
async def on_stop(
    bus: Annotated[PgMessageBus, Depends(runtime.message_bus)],
    instance: Annotated[str, Depends(runtime.instance_name)],
):
    """Кнопка Stop: свой ход обрывается сразу, чужой получает команду через шину."""
    session = current_session()
    thread_id = session.thread_id
    if thread_id is None:
        return

    if ChatTurn.stop(thread_id):
        return

    user_id = session.user_id
    if user_id is None:
        logger.info("stop pressed for thread %s without a user", thread_id)
        return

    command = StopRequested(by_user=int(user_id), by_instance=instance)
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

    return await StreamActions.show(str(user_id), thread_id, action.payload)


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

    return await StreamActions.window(str(user_id), thread_id, action.payload)


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
    graph: Annotated[CompiledStateGraph, Depends(langchain_agent, scope="session")],
):
    """Вкладка вернулась к треду: если ход жив — сохранить loading и живые шаги.

    task_start уже отправлен обёрткой chainlit вокруг хендлера; её же task_end
    глушится, пока ход не закончится. Незавершённых шагов ещё нет в истории,
    а stream_token дописывает только в существующее сообщение — подкладываем
    их в ленту до её отправки клиенту.
    """
    thread_id = thread_dict[ThreadField.ID]
    turn = ChatTurn.active(thread_id)
    renderer = ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)

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
        await InterruptedTurn(graph, thread_id).remember(caught.interrupted)

    if not caught.alive:
        return

    renderer.resume_into(thread_dict)
    ThreadRoom.keep_loading()
