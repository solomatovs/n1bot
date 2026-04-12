"""Boba Chainlit UI — точка входа.

Workspace = Chat. Каждый workspace хранит один чат.
"New Chat" → авто-создание workspace. Sidebar → навигация.
ChatSettings (sidebar) → переименование + модель.
"""
from __future__ import annotations

import asyncio
from queue import Queue
from threading import Thread

import chainlit as cl
from chainlit.types import ThreadDict

from boba_adapters.json_thread_store import JsonThreadStore
from boba_adapters.litellm_models import fetch_chat_models
from boba_app.agent.agent_loop import AgentLoop
from boba_app.session import ChatSession
from boba_chainlit.data_layer import ChainlitDataLayerAdapter
from boba_chainlit.workspace import (
    SessionState,
    WorkspaceError,
    WorkspaceFailure,
    WorkspaceService,
    get_session_state,
    parse_thread_metadata,
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
from boba_domain.config import AppConfig
from boba_domain.di_types import FolderContext
from boba_infra.container import create_container

container = create_container()
cfg = container.get(AppConfig)
agent_cfg = AgentConfig.from_env()
workspace_svc = WorkspaceService(cfg)

_SENTINEL = object()


def _model_widget():
    models = fetch_chat_models(cfg)
    default = agent_cfg.default_model
    if models:
        if default and default not in models:
            models.insert(0, default)
        return cl.input_widget.Select(
            id="model",
            label="Модель",
            values=models,
            initial_value=default if default in models else models[0],
        )
    return cl.input_widget.TextInput(id="model", label="Модель", initial=default)


def _settings_widgets() -> list[cl.input_widget.InputWidget]:
    return [_model_widget()]


def _get_dl():
    from chainlit.data import get_data_layer as _dl
    return _dl()


async def _save_thread_metadata(state: SessionState) -> None:
    """Сохранить state в thread metadata через data layer."""
    if not state.folder:
        return
    data_layer = _get_dl()
    thread_id = cl.context.session.thread_id
    if data_layer and thread_id:
        await data_layer.update_thread(
            thread_id,
            name=state.display_name,
            metadata={"folder": state.folder, "model": state.model},
        )


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------


@cl.header_auth_callback
async def header_auth(_headers) -> cl.User:
    return cl.User(identifier="default", metadata={"role": "user"})


# ------------------------------------------------------------------
# Data Layer
# ------------------------------------------------------------------


@cl.data_layer
def get_data_layer():
    return ChainlitDataLayerAdapter(JsonThreadStore(cfg))


# ------------------------------------------------------------------
# Chat lifecycle
# ------------------------------------------------------------------


@cl.on_chat_start
async def on_chat_start():
    # Workspace создаётся лениво при первом сообщении (_ensure_workspace).
    # Здесь только инициализируем настройки сессии.
    state = SessionState(
        folder="",
        display_name="",
        model=agent_cfg.default_model,
    )
    set_session_state(state)

    settings = await cl.ChatSettings(_settings_widgets()).send()
    model = settings.get("model")
    if model:
        state = SessionState(folder="", display_name="", model=model)
        set_session_state(state)


async def _ensure_workspace() -> SessionState:
    """Создать workspace при первом сообщении, если ещё не создан."""
    state = get_session_state()
    if state is not None and state.folder:
        return state

    result = workspace_svc.create()
    thread_id = cl.context.session.thread_id
    ChatSession.create(cfg, result.folder, chat_id=thread_id)

    model = state.model if state else agent_cfg.default_model
    new_state = SessionState(
        folder=result.folder,
        display_name=result.display_name,
        model=model,
    )
    set_session_state(new_state)
    await _save_thread_metadata(new_state)
    return new_state


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    metadata = parse_thread_metadata(thread)
    if metadata is None:
        await send_error(WorkspaceFailure(WorkspaceError.CORRUPT_METADATA))
        return

    state = SessionState(
        folder=metadata["folder"],
        display_name=thread.get("name") or metadata["folder"],
        model=metadata.get("model", agent_cfg.default_model),
    )
    set_session_state(state)

    await cl.ChatSettings(_settings_widgets()).send()


@cl.on_settings_update
async def on_settings_update(settings: dict):
    state = get_session_state()
    if state is None:
        await send_error(WorkspaceFailure(WorkspaceError.NOT_FOUND))
        return

    model = settings.get("model")
    if model:
        state.model = model

    set_session_state(state)
    await _save_thread_metadata(state)


# ------------------------------------------------------------------
# Message handling
# ------------------------------------------------------------------


@cl.on_message
async def on_message(message: cl.Message):
    state = await _ensure_workspace()

    thread_id = cl.context.session.thread_id
    session = ChatSession.create(cfg, state.folder, chat_id=thread_id)

    q: Queue = Queue()

    def _run_agent():
        try:
            with container(context={FolderContext: session.folder_context}) as scope:
                agent = scope.get(AgentLoop)
                from boba_domain.agent.config import AgentRequest
                for event in agent.run(AgentRequest(query=message.content, model=state.model)):
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

            case ToolResultReady(tool_name=name, content=content):
                async with cl.Step(name=f"✓ {name}") as step:
                    step.output = content[:500]

            case GenerationDone():
                if not answer_started and thinking:
                    msg.content = "".join(thinking)
                    await msg.send()

    if not msg.content:
        msg.content = "(пустой ответ)"
        await msg.send()

    bg.join(timeout=5)
