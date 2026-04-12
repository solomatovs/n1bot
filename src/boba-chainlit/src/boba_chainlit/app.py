"""Boba Chainlit UI — точка входа.

Workspace = Chat. Каждый workspace хранит один чат.
"New Chat" → авто-создание workspace. Sidebar → навигация.
ChatSettings (sidebar) → переименование + модель.
"""
from __future__ import annotations

import asyncio
import re
from queue import Queue
from threading import Thread

import chainlit as cl
from chainlit.types import ThreadDict

from boba_chainlit.data_layer import ChainlitDataLayerAdapter
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
_VALID_NAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁ0-9][a-zA-Zа-яА-ЯёЁ0-9 _\-\.]*$")


def _make_store() -> JsonThreadStore:
    return JsonThreadStore(cfg)


def _get_dl():
    from chainlit.data import get_data_layer as _dl
    return _dl()


def _next_workspace_name() -> str:
    existing = {d.name for d in cfg.iter_workspaces()}
    n = 1
    while f"workspace-{n}" in existing:
        n += 1
    return f"workspace-{n}"


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


def _settings_widgets(folder_name: str):
    return [
        cl.input_widget.TextInput(
            id="workspace_name",
            label="Имя пространства",
            initial=folder_name,
        ),
        _model_widget(),
    ]


# ------------------------------------------------------------------
# Auth — без логина, но Chainlit требует user для sidebar (list_threads)
# ------------------------------------------------------------------


@cl.header_auth_callback
async def header_auth(_headers) -> cl.User:
    return cl.User(identifier="default", metadata={"role": "user"})


# ------------------------------------------------------------------
# Data Layer
# ------------------------------------------------------------------


@cl.data_layer
def get_data_layer():
    return ChainlitDataLayerAdapter(_make_store())


# ------------------------------------------------------------------
# Chat lifecycle
# ------------------------------------------------------------------


@cl.on_chat_start
async def on_chat_start():
    folder_name = _next_workspace_name()
    thread_id = cl.context.session.thread_id
    ChatSession.create(cfg, folder_name, chat_id=thread_id)
    cl.user_session.set("folder", folder_name)

    settings = await cl.ChatSettings(_settings_widgets(folder_name)).send()
    model = settings.get("model", agent_cfg.default_model)
    cl.user_session.set("model", model)

    data_layer = _get_dl()
    if data_layer and thread_id:
        await data_layer.update_thread(
            thread_id,
            name=folder_name,
            metadata={"folder": folder_name, "model": model},
        )


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json
        metadata = json.loads(metadata)

    folder = metadata.get("folder", "")
    model = metadata.get("model", agent_cfg.default_model)
    cl.user_session.set("folder", folder)
    cl.user_session.set("model", model)

    await cl.ChatSettings(_settings_widgets(folder)).send()


@cl.on_settings_update
async def on_settings_update(settings: dict):
    model = settings.get("model", agent_cfg.default_model)
    cl.user_session.set("model", model)

    old_folder = cl.user_session.get("folder") or ""
    new_folder = (settings.get("workspace_name") or "").strip()

    # Переименование workspace
    if new_folder and new_folder != old_folder:
        if not _VALID_NAME_RE.match(new_folder):
            await cl.Message(content=f"Недопустимое имя: **{new_folder}**").send()
        elif cfg.folder_path(new_folder).exists():
            await cl.Message(
                content=f"Пространство **{new_folder}** уже существует."
            ).send()
        else:
            old_path = cfg.folder_path(old_folder)
            if old_path.is_dir():
                old_path.rename(cfg.folder_path(new_folder))
                cl.user_session.set("folder", new_folder)

    folder = cl.user_session.get("folder") or ""
    thread_id = cl.context.session.thread_id
    data_layer = _get_dl()
    if data_layer and thread_id:
        await data_layer.update_thread(
            thread_id,
            name=folder,
            metadata={"folder": folder, "model": model},
        )


# ------------------------------------------------------------------
# Message handling
# ------------------------------------------------------------------


@cl.on_message
async def on_message(message: cl.Message):
    folder_name = cl.user_session.get("folder")
    model = cl.user_session.get("model") or None

    if not folder_name:
        await cl.Message(
            content="Рабочее пространство не выбрано. Создайте новый чат."
        ).send()
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
