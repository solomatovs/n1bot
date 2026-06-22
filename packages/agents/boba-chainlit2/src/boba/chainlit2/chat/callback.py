from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

import chainlit as cl
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit2.chat.handler import chainlit_error_ctx_handler
from boba.chainlit2.chat.tracer import BobaLangchainTracer
from boba.chainlit2.infra.di import Depends, di_inject
from boba.chainlit2.infra.providers import langchain_agent


@cl.on_message
@chainlit_error_ctx_handler
@di_inject
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
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start():
    app_user: cl.User | cl.PersistedUser | None = cl.user_session.get("user")
    await cl.Message(f"Hello {app_user}").send()
