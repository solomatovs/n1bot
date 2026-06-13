from collections.abc import Iterator
from typing import Annotated, Any, cast

import chainlit as cl
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import MessagesState
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit2.infra.di import Depend
from boba.chainlit2.infra.providers import langchain_graph


@cl.on_message
async def on_message(
    msg: cl.Message,
    graph: Annotated[
        CompiledStateGraph[MessagesState, None, MessagesState, MessagesState],
        Depend(langchain_graph, scope="session"),
    ],
):
    cb = cl.LangchainCallbackHandler()
    run_config = RunnableConfig(
        callbacks=[cb],
        configurable={"thread_id": cl.context.session.id},
    )
    final_answer = cl.Message(content="")

    # stream(stream_mode="messages") отдаёт (message_chunk, metadata); типизация
    # langgraph здесь широкая, фиксируем элемент явно
    stream = cast(
        "Iterator[tuple[BaseMessage, dict[str, Any]]]",
        graph.stream(
            {"messages": [HumanMessage(content=msg.content)]},
            stream_mode="messages",
            config=run_config,
        ),
    )
    for chunk, metadata in stream:
        if (
            isinstance(chunk.content, str)
            and chunk.content
            and not isinstance(chunk, HumanMessage)
            and metadata["langgraph_node"] == "final"
        ):
            await final_answer.stream_token(chunk.content)

    await final_answer.send()
