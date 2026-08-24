"""Callback'и chainlit: мост между интерфейсом чата и агентом langgraph."""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import Request, Response
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.chainlit.canvas.panel import (
    CanvasAction,
    RenderVerdicts,
    StreamActions,
)
from boba.chainlit.canvas.tools import CanvasActions
from boba.chainlit.chat.history import GraphTurnHistory, ThreadRewind
from boba.chainlit.chat.panel_text import PanelText
from boba.chainlit.chat.settings import SettingsPanel
from boba.chainlit.chat.tracing import LlmStateLog
from boba.chainlit.chat.turn import ChatTurn
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.domain.errors import InternalServiceError
from boba.chainlit.domain.fields import ThreadField
from boba.chainlit.domain.session import (
    LogUserMark,
    UserMetadataField,
)
from boba.chainlit.infra import providers
from boba.chainlit.infra.config import (
    AppConfig,
    ChatProfiles,
    SelectedProfile,
    SettingsView,
    UserLlmOverrides,
    UserMeta,
)
from boba.chainlit.infra.di import Container, Depends, di_inject
from boba.chainlit.infra.providers import (
    chainlit_data_layer,
    chat_profiles_registry,
    get_app_config,
    langchain_agent,
)
from boba.chainlit.infra.session import (
    ChainlitSession,
    current_session,
    session_source_ref,
)
from boba.chainlit.infra.thread_room import ThreadRoom
from boba.chainlit.infra.user_connections import UserKerberos
from boba.chainlit.rendering.chat_view import ChatView, LiveSink
from boba.chainlit.rendering.errors import chainlit_error_ctx_handler
from chainlit.auth.cookie import clear_auth_cookie, get_token_from_cookies
from chainlit.config import config as chainlit_config
from chainlit.data.base import BaseDataLayer
from chainlit.input_widget import Tab
from chainlit.types import ThreadDict
from chainlit.utils import wrap_user_function

logger = logging.getLogger(__name__)


@chainlit_error_ctx_handler
@di_inject
async def on_message(
    msg: cl.Message,
    graph: Annotated[
        CompiledStateGraph,
        Depends(langchain_agent, scope="session"),
    ],
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
):
    thread_id = current_session().thread_id
    if thread_id is None:
        raise InternalServiceError(
            internal_detail="on_message outside a chainlit thread",
            user_detail=None,
        )

    ThreadRoom.activate(thread_id)

    view = ChatView(thread_id, LiveSink())
    view.begin_turn(msg.id)
    turn = ChatTurn(
        thread_id=thread_id,
        view=view,
        history=GraphTurnHistory(graph, thread_id),
        key=msg.id,
    )

    # сбой в любом месте хода — включая подготовку — отчитывается ходом же:
    # чат, история и журнал получают одну формулировку
    try:
        rewind = ThreadRewind(graph, data_layer, thread_id)
        if await rewind.is_edit(msg.id):
            await rewind.apply(msg.id, msg.content)
            await rewind.refresh_view()

        state_log = LlmStateLog(LogUserMark(current_session().label, thread_id))
        run_config = RunnableConfig(
            callbacks=[turn.tracer, state_log],
            configurable={"thread_id": thread_id},
        )

        stream = cast(
            "AsyncIterator[tuple[BaseMessage, dict[str, Any]]]",
            graph.astream(
                {"messages": [ChatTurn.human_message(msg, session_source_ref())]},
                stream_mode="messages",
                config=run_config,
            ),
        )

        await turn.run(stream)
    except Exception as e:
        await turn.crash(e)


chainlit_config.code.on_message = wrap_user_function(on_message)


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
    # делегированный тикет входа гаснет вместе с сессией, не дожидаясь срока JWT
    if token := get_token_from_cookies(request.cookies):
        UserKerberos(providers.ccache_registry_ref).forget(token)

    # только свои: на домене живут и чужие приложения, а среди присланных
    # кук попадаются имена, которых http.cookies не принимает ('Path')
    clear_auth_cookie(request, response)


@cl.on_stop
async def on_stop():
    """Кнопка Stop: обрываем ход треда, а не надеемся на отмену задачи chainlit."""
    thread_id = current_session().thread_id
    if thread_id is None:
        return
    if not ChatTurn.stop(thread_id):
        logger.info("stop pressed for thread %s: nothing is running", thread_id)


@cl.data_layer
@di_inject
def get_data_layer(
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
) -> BaseDataLayer:
    return data_layer


@cl.action_callback(CanvasAction.OPEN)
@chainlit_error_ctx_handler
async def on_canvas_open(action: cl.Action) -> None:
    """Клик по ссылке в переписке открывает панель без участия агента."""
    await CanvasActions.open(action)


@cl.action_callback(CanvasAction.CONTENT)
@chainlit_error_ctx_handler
async def on_canvas_content(action: cl.Action) -> dict[str, Any]:
    """Панель уже открыта: отдаём описание файла, не подменяя элемент."""
    return await CanvasActions.content(action)


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
async def on_chat_resume(thread_dict: ThreadDict):
    """Вкладка вернулась к треду: если ход жив — сохранить loading и живые шаги.

    task_start уже отправлен обёрткой chainlit вокруг хендлера; её же task_end
    глушится, пока ход не закончится. Незавершённых шагов ещё нет в истории,
    а stream_token дописывает только в существующее сообщение — подкладываем
    их в ленту до её отправки клиенту.
    """
    thread_id = thread_dict[ThreadField.ID]
    turn = ChatTurn.active(thread_id)

    room: list[str] = []
    for session in ThreadRoom.sessions(thread_id):
        room.append(session.id)

    turn_state = "none"
    if turn is not None:
        turn_state = "alive"

    logger.info(
        "resume thread %s: turn=%s, thread sessions=%s, current session=%s",
        thread_id,
        turn_state,
        room,
        current_session().id,
    )

    if turn is None:
        return

    turn.resume_into(thread_dict)
    ThreadRoom.keep_loading()
