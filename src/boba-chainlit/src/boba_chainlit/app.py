"""Boba Chainlit UI — точка входа."""
from __future__ import annotations

import asyncio
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Optional

import chainlit as cl
from chainlit.types import ChatProfile, ThreadDict
from chainlit.user import User

from boba_adapters.chainlit_data_layer import ChainlitDataLayerAdapter
from boba_adapters.json_thread_store import JsonThreadStore
from boba_adapters.litellm_models import fetch_chat_models
from boba_app.agent.agent_loop import AgentLoop
from boba_app.session import ChatSession
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

_SENTINEL = object()


# ------------------------------------------------------------------
# Data Layer
# ------------------------------------------------------------------


@cl.data_layer
def get_data_layer():
    store = JsonThreadStore(
        base_dir=Path(cfg.import_base_dir),
        boba_dir_name=cfg.boba_dir_name,
    )
    return ChainlitDataLayerAdapter(store)


# ------------------------------------------------------------------
# Chat Profiles — каждая папка с документами = отдельный профиль
# ------------------------------------------------------------------


@cl.set_chat_profiles
async def chat_profiles(_current_user: Optional[User] = None):
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name
        for d in base_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )

    return [
        ChatProfile(
            name=folder,
            markdown_description=f"Документы: **{folder}**",
            default=(i == 0),
        )
        for i, folder in enumerate(folders)
    ]


# ------------------------------------------------------------------
# Chat lifecycle
# ------------------------------------------------------------------


@cl.on_chat_start
async def on_chat_start():
    folder = cl.user_session.get("chat_profile")
    cl.user_session.set("folder", folder)
    cl.user_session.set("model", agent_cfg.default_model)

    # Модель — через ChatSettings (можно менять в ходе чата)
    models = fetch_chat_models(cfg)
    default = agent_cfg.default_model

    if models:
        if default and default not in models:
            models.insert(0, default)
        widget = cl.input_widget.Select(
            id="model",
            label="Модель",
            values=models,
            initial_value=default if default in models else models[0],
        )
    else:
        widget = cl.input_widget.TextInput(
            id="model", label="Модель", initial=default
        )

    await cl.ChatSettings([widget]).send()

    # Сохраняем folder в thread metadata
    thread_id = cl.context.session.thread_id
    from chainlit.data import get_data_layer as _get_dl

    data_layer = _get_dl()
    if data_layer and thread_id:
        await data_layer.update_thread(
            thread_id, metadata={"folder": folder, "model": default}
        )


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json

        metadata = json.loads(metadata)
    cl.user_session.set("folder", metadata.get("folder", ""))
    cl.user_session.set("model", metadata.get("model", agent_cfg.default_model))


@cl.on_settings_update
async def on_settings_update(settings: dict):
    model = settings.get("model", agent_cfg.default_model)
    cl.user_session.set("model", model)

    thread_id = cl.context.session.thread_id
    from chainlit.data import get_data_layer as _get_dl

    data_layer = _get_dl()
    if data_layer and thread_id:
        await data_layer.update_thread(thread_id, metadata={"model": model})


# ------------------------------------------------------------------
# Message handling
# ------------------------------------------------------------------


@cl.on_message
async def on_message(message: cl.Message):
    folder_name = cl.user_session.get("folder")
    model = cl.user_session.get("model") or None

    if not folder_name:
        await cl.Message(content="Выберите папку в профиле чата.").send()
        return

    thread_id = cl.context.session.thread_id
    session = ChatSession.create(cfg, folder_name, chat_id=thread_id)

    q: Queue = Queue()

    def _run_agent():
        try:
            with container(context={FolderContext: session.folder_context}) as scope:
                agent = scope.get(AgentLoop)
                for event in agent.run(message.content, model):
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
            await cl.Message(content=f"Ошибка: {event}").send()
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
