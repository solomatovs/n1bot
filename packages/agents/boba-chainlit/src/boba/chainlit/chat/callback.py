"""Callback'и chainlit: мост между интерфейсом чата и агентом langgraph."""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import Request, Response
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.chainlit.agent.tools.canvas import (
    canvas_content_action,
    open_canvas_action,
)
from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.chat.data.fields import StepField, ThreadField
from boba.chainlit.chat.edit import ThreadRewind
from boba.chainlit.chat.errors import chainlit_error_ctx_handler
from boba.chainlit.chat.turn import ChatTurn, ThreadRoom
from boba.chainlit.infra.di import Depends, di_inject
from boba.chainlit.infra.providers import chainlit_data_layer, langchain_agent
from boba.chainlit.infra.session import current_thread_id
from boba.chainlit.rendering.canvas import CanvasAction, RenderVerdicts
from boba.chainlit.rendering.chat_view import ChatView, LiveSink
from chainlit.config import config as chainlit_config
from chainlit.data.base import BaseDataLayer
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
    thread_id = cl.context.session.thread_id
    ThreadRoom.activate(thread_id)

    rewind = ThreadRewind(graph, data_layer, thread_id)
    if await rewind.is_edit(msg.id):
        await rewind.apply(msg.id, msg.content)
        await rewind.refresh_view()

    view = ChatView(thread_id, LiveSink())
    view.begin_turn(msg.id)
    tracer = AgentTracer(view)
    run_config = RunnableConfig(
        callbacks=[tracer],
        configurable={"thread_id": thread_id},
    )

    stream = cast(
        "AsyncIterator[tuple[BaseMessage, dict[str, Any]]]",
        graph.astream(
            {"messages": [ChatTurn.human_message(msg)]},
            stream_mode="messages",
            config=run_config,
        ),
    )

    await ChatTurn(graph, thread_id, view, tracer, msg.id).run(stream)


chainlit_config.code.on_message = wrap_user_function(on_message)


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start():
    session = cl.context.session
    logger.info(
        "chat start: session=%s, thread=%s", session.id, session.thread_id
    )


@cl.on_logout
def on_logout(request: Request, response: Response):
    for cookie_name in request.cookies:
        response.delete_cookie(cookie_name)


@cl.on_stop
async def on_stop():
    """Кнопка Stop: обрываем ход треда, а не надеемся на отмену задачи chainlit."""
    thread_id = current_thread_id()
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
    await open_canvas_action(action)


@cl.action_callback(CanvasAction.CONTENT)
@chainlit_error_ctx_handler
async def on_canvas_content(action: cl.Action) -> dict[str, Any]:
    """Панель уже открыта: отдаём описание файла, не подменяя элемент."""
    return await canvas_content_action(action)


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
        cl.context.session.id,
    )
    if turn is None:
        return

    live = turn.resume_steps()
    steps = list(thread_dict.get(ThreadField.STEPS) or [])
    positions: dict[str, int] = {}
    for index, step in enumerate(steps):
        positions[step.get(StepField.ID, "")] = index
    for step in live:
        index = positions.get(step.get(StepField.ID, ""))
        if index is None:
            steps.append(step)
        else:
            steps[index] = step
    thread_dict[ThreadField.STEPS] = steps
    names: list[str] = []
    for step in live:
        names.append(str(step.get(StepField.NAME, "")))
    logger.info(
        "resume thread %s: %d live steps merged (%s)",
        thread_id,
        len(live),
        ", ".join(names),
    )

    ThreadRoom.keep_loading()
