"""Callback'и chainlit: мост между интерфейсом чата и агентом langgraph.

Здесь живут точки входа chainlit (сообщение, старт/конец чата, восстановление
треда, data layer). Отрисовку ленты ведёт ChatView, историю — checkpointer.
"""

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.types import ThreadDict
from chainlit.user_session import UserSession
from fastapi import Request, Response
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit2.chat.agent_tracer import AgentTracer
from boba.chainlit2.chat.handler import chainlit_error_ctx_handler
from boba.chainlit2.infra.di import Depends, di_inject
from boba.chainlit2.infra.providers import chainlit_data_layer, langchain_agent
from boba.chainlit2.rendering.chat_view import ChatView, LiveSink

logger = logging.getLogger(__name__)


class ChainlitAdapter:
    "Мост взаимодействия бизнес логики и chainlit"

    @staticmethod
    def get_chat_id(session: HTTPSession | WebsocketSession):
        return session.thread_id

    @staticmethod
    def get_chat_user(session: UserSession):
        if user := session.get("user"):
            if not isinstance(user, (cl.PersistedUser, cl.User)):
                raise RuntimeError(f"user in user_session is not valud: {type(user)}")

            return user

        raise RuntimeError("user does't exists in session")

    @staticmethod
    async def report_failure(
        graph: CompiledStateGraph,
        thread_id: str,
        view: ChatView,
        key: str,
        error: BaseException,
    ) -> None:
        text = f"**сбой:** {error}"
        await view.error(text, key)
        await graph.aupdate_state(
            RunnableConfig(configurable={"thread_id": thread_id}),
            {
                "messages": [
                    AIMessage(content=text, additional_kwargs={"error": True})
                ]
            },
        )


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
    thread_id = ChainlitAdapter.get_chat_id(cl.context.session)

    view = ChatView(thread_id, LiveSink())
    run_config = RunnableConfig(
        callbacks=[AgentTracer(view)],
        configurable={"thread_id": thread_id},
    )

    stream = cast(
        "AsyncIterator[tuple[BaseMessage, dict[str, Any]]]",
        graph.astream(
            {"messages": [HumanMessage(content=msg.content, id=msg.id)]},
            stream_mode="messages",
            config=run_config,
        ),
    )

    final_answer: cl.Message | None = None

    async def _final_message() -> cl.Message:
        nonlocal final_answer
        if final_answer is None:
            final_answer = view.open_answer(msg.id)
        return final_answer

    try:
        async for chunk, _metadata in stream:
            if (
                isinstance(chunk, AIMessageChunk)
                and isinstance(chunk.content, str)
                and chunk.content
            ):
                answer = await _final_message()
                await answer.stream_token(chunk.content)
    except Exception as e:
        logger.exception("агент не отработал")
        await ChainlitAdapter.report_failure(graph, thread_id, view, msg.id, e)
        return

    if final_answer is None:
        final_answer = view.open_answer(msg.id)
    await final_answer.send()


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start():
    pass


@cl.on_logout
def on_logout(request: Request, response: Response):
    for cookie_name in request.cookies:
        response.delete_cookie(cookie_name)


@cl.on_stop
async def on_stop():
    user = ChainlitAdapter.get_chat_user(cl.user_session)
    logger.info(f"{user.identifier} has stopped the task!")
    await cl.Message("You have stopped the task!").send()


@cl.on_chat_end
def on_chat_end():
    user = ChainlitAdapter.get_chat_user(cl.user_session)
    logger.info(f"{user.identifier} has ended the chat")


@cl.data_layer
@di_inject
def get_data_layer(
    data_layer: Annotated[BaseDataLayer, Depends(chainlit_data_layer)],
) -> BaseDataLayer:
    return data_layer


@cl.on_chat_resume
@chainlit_error_ctx_handler
async def on_chat_resume(thread_dict: ThreadDict):
    pass
