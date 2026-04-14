"""Boba Chainlit UI — точка входа.

Workspace = Chat. Каждый workspace хранит один чат.
"New Chat" → авто-создание workspace. Sidebar → навигация.
ChatSettings (sidebar) → переименование + модель.
"""

from __future__ import annotations

import asyncio
import logging
from queue import Queue
from threading import Thread

import chainlit as cl
from chainlit.types import ThreadDict

from boba_adapters.litellm_models import fetch_chat_models
from boba_app.agent.agent_loop import AgentLoop
from boba_app.session import ChatSession
from boba_chainlit.data_layer import ChainlitDataLayerAdapter
from boba_chainlit.workspace import (
    DataLayerNotInitializedError,
    ParsedChatSettings,
    SessionNotInitializedError,
    SessionRegistry,
    SessionState,
    ThreadId,
    WorkspaceError,
    WorkspaceFailure,
    WorkspaceService,
    get_current_user_id,
    get_session_state,
    send_error,
    set_session_state,
)
from boba_domain.agent.config import AgentConfig
from boba_domain.agent.events import (
    AnswerToken,
    GenerationDone,
    ThinkingToken,
    ToolCallStarted,
    ToolResultReady,
)
from boba_domain.agent.config import AgentRequest
from boba_domain.config import AppConfig
from boba_domain.core.thread_store import ChatThreadStore
from boba_domain.di_types import WorkspaceContext
from boba_infra.container import create_container

log = logging.getLogger(__name__)

container = create_container()
cfg = container.get(AppConfig)
thread_store = container.get(ChatThreadStore)
agent_cfg = AgentConfig.from_env()
workspace_svc = WorkspaceService(cfg, thread_store)
session_registry = SessionRegistry()

_SENTINEL = object()


def _model_widget(initial: str):
    """Виджет выбора модели. Если есть список моделей — Select, иначе TextInput."""
    models = fetch_chat_models(cfg)
    if models:
        if initial and initial not in models:
            models.insert(0, initial)
        return cl.input_widget.Select(
            id="model",
            label="Модель",
            values=models,
            initial_value=initial if initial in models else models[0],
        )
    return cl.input_widget.TextInput(id="model", label="Модель", initial=initial)


def _max_tokens_widget(initial: int):
    """Виджет для настройки max tokens."""
    return cl.input_widget.NumberInput(
        id="max_tokens",
        label="Контекстное окно (токены)",
        initial=initial,
    )


def _settings_widgets(model: str, max_tokens: int) -> list[cl.input_widget.InputWidget]:
    """Виджеты для настроек чата, инициализированные из WorkspaceSettings."""
    return [
        _model_widget(initial=model),
        _max_tokens_widget(initial=max_tokens),
    ]


def _get_dl():
    """Data layer. Всегда доступен — зарегистрирован через @cl.data_layer."""
    from chainlit.data import get_data_layer

    dl = get_data_layer()
    if dl is None:
        raise DataLayerNotInitializedError()
    
    return dl


async def _save_thread_metadata(state: SessionState) -> None:
    """Сохранить state в thread metadata через data layer."""
    await _get_dl().update_thread(
        thread_id=state.folder.value,
        name=state.display_name,
        user_id=state.user_id,
        metadata=state.to_thread_metadata(),
        tags=state.tags,
    )


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------


@cl.header_auth_callback
async def header_auth(_headers) -> cl.User:
    """Авторизация. Вызывается Chainlit при каждом WebSocket-подключении."""
    return cl.User(identifier="default", metadata={"role": "user"})


# ------------------------------------------------------------------
# Data Layer
# ------------------------------------------------------------------


@cl.data_layer
def get_data_layer():
    return ChainlitDataLayerAdapter(thread_store, session_registry)


# ------------------------------------------------------------------
# Chat lifecycle
# ------------------------------------------------------------------


async def _init_session() -> None:
    """Общая логика инициализации сессии (новый чат и resume).

    1. Ensure workspace — единственная точка создания и чтения настроек
    2. Привязать thread_id → folder в registry
    3. Отправить ChatSettings виджеты (инициализированные из workspace)
    4. Заполнить SessionState
    5. Сохранить metadata
    """
    thread_id = ThreadId(cl.context.session.thread_id)
    ws = workspace_svc.ensure_or_first()
    session_registry.bind(thread_id, ws.folder)

    model = ws.model or agent_cfg.default_model
    raw_settings = await cl.ChatSettings(
        _settings_widgets(model=model, max_tokens=ws.max_tokens)
    ).send()
    chat_settings = ParsedChatSettings.from_raw(raw_settings, model)

    state = SessionState(
        folder=ws.folder,
        display_name=ws.display_name,
        model=chat_settings.model,
        user_id=get_current_user_id(),
        max_tokens=chat_settings.max_tokens,
    )
    set_session_state(state)
    await _save_thread_metadata(state)


@cl.on_chat_start
async def on_chat_start():
    """Новый чат. Вызывается Chainlit при:
    — первом визите (нет thread_id от frontend)
    — нажатии кнопки "New Chat"
    — восстановлении WebSocket-сессии (в пределах session_timeout),
      если пользователь ещё не отправлял сообщений
    """
    await _init_session()


@cl.on_chat_resume
async def on_chat_resume(_thread: ThreadDict):
    """Возврат к существующему чату. Вызывается Chainlit когда
    frontend передаёт thread_id (клик по чату в sidebar).
    Chainlit загружает thread из data layer и передаёт сюда.
    """
    await _init_session()


@cl.on_settings_update
async def on_settings_update(settings: dict):
    """Пользователь изменил настройки чата (напр. сменил модель)."""
    try:
        state = get_session_state()
    except SessionNotInitializedError as e:
        log.error("on_settings_update: %s", e)
        await send_error(WorkspaceFailure(WorkspaceError.NOT_FOUND))
        return

    chat_settings = ParsedChatSettings.from_raw(settings, state.model)
    state.model = chat_settings.model
    state.max_tokens = chat_settings.max_tokens

    set_session_state(state)
    await _save_thread_metadata(state)


# ------------------------------------------------------------------
# Message handling
# ------------------------------------------------------------------


@cl.on_message
async def on_message(message: cl.Message):
    """Пользователь отправил сообщение. Вызывается Chainlit
    при каждом сообщении в активном чате (новом или возобновлённом).
    """
    try:
        state = get_session_state()
    except SessionNotInitializedError as e:
        log.error("on_message: %s", e)
        await send_error(WorkspaceFailure(WorkspaceError.NOT_FOUND))
        return

    session = ChatSession.from_folder(cfg, state.folder.value)

    q: Queue = Queue()

    def _run_agent():
        try:
            with container(
                context={WorkspaceContext: session.workspace_context}
            ) as scope:
                agent = scope.get(AgentLoop)
                

                for event in agent.run(AgentRequest(
                    query=message.content,
                    model=state.model,
                    max_tokens=state.max_tokens,
                )):
                    q.put(event)
        except Exception as e:
            q.put(e)
        finally:
            q.put(_SENTINEL)

    bg = Thread(target=_run_agent, daemon=True)
    bg.start()

    msg = cl.Message(content="")
    thinking: list[str] = []
    answer_started = False

    while True:
        event = await asyncio.to_thread(q.get)

        if event is _SENTINEL:
            break
        if isinstance(event, Exception):
            await cl.Message(content=f"Ошибка агента: {type(event).__name__}").send()
            break

        match event:
            case ThinkingToken(token=tok):
                thinking.append(tok)

            case AnswerToken(token=tok):
                if not answer_started:
                    if thinking:
                        async with cl.Step(name="Размышления") as step:
                            step.output = "".join(thinking)
                    answer_started = True
                msg.content += tok
                await msg.send()

            case ToolCallStarted(tool_name=name, arguments=args):
                async with cl.Step(name=f"🔧 {name}") as step:
                    step.input = args

            case ToolResultReady(tool_name=name, content=content, is_error=err):
                icon = "✗" if err else "✓"
                async with cl.Step(name=f"{icon} {name}") as step:
                    step.output = content[:500]

            case GenerationDone():
                if not answer_started and thinking:
                    msg.content = "".join(thinking)
                    await msg.send()

    if not msg.content:
        msg.content = "(пустой ответ)"
        await msg.send()

    bg.join(timeout=5)
