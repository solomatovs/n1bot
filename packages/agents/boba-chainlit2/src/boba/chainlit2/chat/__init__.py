import functools
import logging
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, cast

import chainlit as cl
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit2.chat.tracer import BobaLangchainTracer
from boba.chainlit2.infra.di import Depends, inject
from boba.chainlit2.infra.providers import langchain_agent

logger = logging.getLogger("chat")


def report_errors(fn: Callable) -> Callable:
    """Ловит ошибку обработки сообщения (вкл. DI-резолв) и сообщает её в чат."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            logger.error(f"on_message failed: {exc}")
            await cl.ErrorMessage(content=f"Failed to process message: {exc}").send()
            return None

    return wrapper


@cl.on_message
@report_errors
@inject
async def on_message(
    msg: cl.Message,
    graph: Annotated[
        CompiledStateGraph,
        Depends(langchain_agent, scope="session"),
    ],
):
    cb = BobaLangchainTracer()
    run_config = RunnableConfig(
        callbacks=[cb],
        configurable={"thread_id": cl.context.session.id},
    )
    final_answer = cl.Message(content="")

    # astream(stream_mode="messages") отдаёт (message_chunk, metadata); типизация
    # langgraph здесь широкая, фиксируем элемент явно
    stream = cast(
        "AsyncIterator[tuple[BaseMessage, dict[str, Any]]]",
        graph.astream(
            {"messages": [HumanMessage(content=msg.content)]},
            stream_mode="messages",
            config=run_config,
        ),
    )
    async for chunk, _metadata in stream:
        if isinstance(chunk.content, str):
            await final_answer.stream_token(chunk.content)

    await final_answer.send()


@cl.on_chat_start
async def on_chat_start():
    app_user: cl.User | cl.PersistedUser | None = cl.user_session.get("user")
    await cl.Message(f"Hello {app_user}").send()
