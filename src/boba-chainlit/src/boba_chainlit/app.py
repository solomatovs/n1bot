"""Boba Chainlit UI — точка входа."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from queue import Queue, Empty
from threading import Thread

import httpx
import chainlit as cl

from boba_app.agent.agent_loop import AgentLoop
from boba_domain.agent.events import (
    AnswerToken,
    GenerationDone,
    ThinkingToken,
    ToolCallStarted,
    ToolResultReady,
)
from boba_domain.agent.config import AgentConfig
from boba_domain.config import AppConfig
from boba_domain.di_types import FolderContext
from boba_infra.container import create_container

log = logging.getLogger(__name__)

container = create_container()
cfg = container.get(AppConfig)
agent_cfg = AgentConfig.from_env()


def _fetch_models() -> list[str]:
    """Получить список доступных моделей из LiteLLM."""
    try:
        r = httpx.get(
            cfg.litellm_models_url,
            headers=cfg.litellm_auth_headers,
            verify=cfg.ssl_verify,
            timeout=10,
        )
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))
    except Exception as e:
        log.warning("Не удалось получить список моделей: %s", e)
        return []

_SENTINEL = object()


@cl.on_chat_start
async def on_chat_start():
    """Инициализация сессии — выбор папки."""
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name for d in base_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )

    if not folders:
        await cl.Message(content="Нет папок с документами. Импортируйте документы.").send()
        return

    models = _fetch_models()
    default_model = agent_cfg.default_model
    if default_model and default_model not in models:
        models.insert(0, default_model)

    widgets = [
        cl.input_widget.Select(
            id="folder",
            label="Папка с документами",
            values=folders,
            initial_value=folders[0],
        ),
    ]
    if models:
        widgets.append(cl.input_widget.Select(
            id="model",
            label="Модель",
            values=models,
            initial_value=default_model if default_model in models else models[0],
        ))
    else:
        widgets.append(cl.input_widget.TextInput(
            id="model",
            label="Модель",
            initial=default_model,
        ))

    settings = await cl.ChatSettings(widgets).send()

    cl.user_session.set("folder", settings["folder"])
    cl.user_session.set("model", settings["model"])


@cl.on_settings_update
async def on_settings_update(settings: dict):
    cl.user_session.set("folder", settings["folder"])
    cl.user_session.set("model", settings["model"])


@cl.on_message
async def on_message(message: cl.Message):
    """Обработка сообщения пользователя — стриминг через Queue."""
    folder_name = cl.user_session.get("folder")
    model = cl.user_session.get("model") or None  # None → AgentLoop возьмёт default

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

    # Queue для стриминга событий из sync-потока в async event loop
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

    # Рендерим события по мере поступления
    msg = cl.Message(content="")
    thinking_content: list[str] = []
    answer_started = False

    while True:
        try:
            event = await asyncio.to_thread(q.get, timeout=0.1)
        except Empty:
            continue

        if event is _SENTINEL:
            break
        if isinstance(event, Exception):
            await cl.Message(content=f"Ошибка: {event}").send()
            break

        match event:
            case ThinkingToken(token=tok):
                thinking_content.append(tok)

            case AnswerToken(token=tok):
                if not answer_started:
                    if thinking_content:
                        async with cl.Step(name="Размышления") as step:
                            step.output = "".join(thinking_content)
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
                if not answer_started and thinking_content:
                    msg.content = "".join(thinking_content)
                    await msg.send()

    if not msg.content:
        msg.content = "(пустой ответ)"
        await msg.send()

    thread.join(timeout=5)
