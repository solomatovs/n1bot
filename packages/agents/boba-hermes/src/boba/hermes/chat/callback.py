import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, ClassVar, cast

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.types import ThreadDict
from chainlit.user_session import UserSession
from fastapi import Request, Response
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from boba.hermes.agent.profile import HermesProfileProvisioner
from boba.hermes.chat.handler import chainlit_error_ctx_handler
from boba.hermes.chat.tracer import BobaLangchainTracer
from boba.hermes.infra.di import Depends, di_inject
from boba.hermes.infra.providers import (
    chainlit_data_layer,
    hermes_profile_provisioner,
    langchain_agent,
)

logger = logging.getLogger(__name__)


class ChainlitAdapter:
    "Мост взаимодействия бизнес логики и chainlit"

    # профиль hermes текущего пользователя; кладётся при входе в чат
    PROFILE_KEY: ClassVar[str] = "hermes_profile"

    @staticmethod
    def get_chat_id(session: HTTPSession | WebsocketSession):
        "возвращает идентификатор чата из сессии chainlit"
        return session.thread_id

    @staticmethod
    def get_chat_user(session: UserSession):
        "возвращает объект описывающий пользователя чата"
        if user := session.get("user"):
            if not isinstance(user, (cl.PersistedUser, cl.User)):
                raise RuntimeError(f"user in user_session is not valud: {type(user)}")

            return user

        raise RuntimeError("user does't exists in session")


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
    # id одного диалога
    thread_id = ChainlitAdapter.get_chat_id(cl.context.session)

    cb = BobaLangchainTracer()
    run_config = RunnableConfig(
        callbacks=[cb],
        # langchain собирает историю из общего массива сообщений пользователей
        # через ключ thread_id, поэтому передаем сюда
        # сессионный ключ chainlit
        configurable={"thread_id": thread_id},
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
async def on_chat_start(
    profiles: Annotated[
        HermesProfileProvisioner,
        Depends(hermes_profile_provisioner, scope="session"),
    ],
):
    user = ChainlitAdapter.get_chat_user(cl.user_session)
    # профиль заводится при входе: у каждого пользователя свой state.db на
    # стороне hermes, обращения идут на /p/<профиль>/
    profile = await profiles.ensure(user.identifier)
    cl.user_session.set(ChainlitAdapter.PROFILE_KEY, profile)
    await cl.Message(f"Hello {user}").send()


@cl.on_logout
def on_logout(request: Request, response: Response):
    """вызывается, когда пользователь нажимает Logout"""
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
@di_inject
async def on_chat_resume(
    thread_dict: ThreadDict,
    graph: Annotated[
        CompiledStateGraph,
        Depends(langchain_agent, scope="session"),
    ],
):
    thread_id = ChainlitAdapter.get_chat_id(cl.context.session)

    cb = BobaLangchainTracer()
    run_config = RunnableConfig(
        callbacks=[cb],
        # langchain собирает историю из общего массива сообщений пользователей
        # через ключ thread_id, поэтому передаем сюда
        # сессионный ключ chainlit
        configurable={"thread_id": thread_id},
    )

    # читаем langgraph состояние с историей сообщений
    _snapshot = await graph.aget_state(run_config)
    # print(f"{snapshot}")

    async for _m in graph.aget_state_history(run_config):
        pass
