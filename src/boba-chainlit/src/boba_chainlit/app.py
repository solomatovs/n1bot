"""Boba Chainlit UI — точка входа."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from queue import Queue
from threading import Thread

import chainlit as cl

from boba_adapters.litellm_models import fetch_chat_models
from boba_app.agent.agent_loop import AgentLoop
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


def _model_selector() -> cl.input_widget.Select | cl.input_widget.TextInput:
    """Виджет выбора модели: Select если LiteLLM доступен, иначе TextInput."""
    models = fetch_chat_models(cfg)
    default = agent_cfg.default_model

    if not models:
        return cl.input_widget.TextInput(id="model", label="Модель", initial=default)

    if default and default not in models:
        models.insert(0, default)

    return cl.input_widget.Select(
        id="model",
        label="Модель",
        values=models,
        initial_value=default if default in models else models[0],
    )


@cl.on_chat_start
async def on_chat_start():
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    if not folders:
        await cl.Message(
            content="Нет папок с документами. Импортируйте документы."
        ).send()
        return

    settings = await cl.ChatSettings(
        [
            cl.input_widget.Select(
                id="folder",
                label="Папка с документами",
                values=folders,
                initial_value=folders[0],
            ),
            _model_selector(),
        ]
    ).send()

    cl.user_session.set("folder", settings["folder"])
    cl.user_session.set("model", settings["model"])


@cl.on_settings_update
async def on_settings_update(settings: dict):
    cl.user_session.set("folder", settings["folder"])
    cl.user_session.set("model", settings["model"])


@cl.on_message
async def on_message(message: cl.Message):
    folder_name = cl.user_session.get("folder")
    model = cl.user_session.get("model") or None

    if not folder_name:
        await cl.Message(content="Выберите папку в настройках.").send()
        return

    folder_path = Path(cfg.import_base_dir) / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    chat_id = uuid.uuid4().hex[:12]
    cfg.boba_path(folder_path).mkdir(parents=True, exist_ok=True)
    cfg.chats_dir(folder_path).mkdir(parents=True, exist_ok=True)
    history_path = cfg.chat_history_path(folder_path, chat_id)
    history_path.touch(exist_ok=True)

    ctx = FolderContext(folder_path=folder_path, history_path=history_path)

    q: Queue = Queue()

    def _run_agent():
        try:
            with container(context={FolderContext: ctx}) as scope:
                agent = scope.get(AgentLoop)
                for event in agent.run(message.content, model):
                    q.put(event)
        except Exception as e:
            q.put(e)
        finally:
            q.put(_SENTINEL)

    thread = Thread(target=_run_agent, daemon=True)
    thread.start()

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

    thread.join(timeout=5)
