import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.types import ThreadDict
from chainlit.user_session import UserSession
from fastapi import Request, Response
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit2.chat.agent_tracer import AgentTracer
from boba.chainlit2.chat.handler import chainlit_error_ctx_handler
from boba.chainlit2.infra.di import Depends, di_inject
from boba.chainlit2.infra.providers import chainlit_data_layer, langchain_agent

logger = logging.getLogger(__name__)


class ChainlitAdapter:
    "Мост взаимодействия бизнес логики и chainlit"

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

    cb = AgentTracer()
    run_config = RunnableConfig(
        callbacks=[cb],
        # langchain собирает историю из общего массива сообщений пользователей
        # через ключ thread_id, поэтому передаем сюда
        # сессионный ключ chainlit
        configurable={"thread_id": thread_id},
    )

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

    # Итоговый ответ создаём лениво — на первом content-чанке модели, а не до
    # запуска цикла. История (и лента) сортируется по created_at: если создать
    # ответ до вызовов инструментов, он встанет раньше шагов процесса, хотя
    # должен идти ПОСЛЕ них. Ленивое создание даёт правильный порядок.
    final_answer: cl.Message | None = None

    async def _final_message() -> cl.Message:
        nonlocal final_answer
        if final_answer is None:
            final_answer = cl.Message(content="")
            # top-level: chainlit по умолчанию вешает cl.Message ребёнком
            # run-step'а @cl.on_message, и фронт позиционирует ответ по
            # родителю (created_at run'а РАНЬШЕ шагов процесса) — ответ
            # всплывает над контейнером. Обнуляем parent — сообщение живёт
            # на верхнем уровне и сортируется по своему created_at.
            final_answer.parent_id = None
        return final_answer

    async for chunk, _metadata in stream:
        # Стримим в ответ только контент модели: stream_mode="messages" отдаёт
        # чанки всех узлов, включая ToolMessage (содержимое результата
        # инструмента). Если стримить их — сырой контент tool-результата
        # (JSON/таблица) попадёт в бабл финального ответа.
        #
        # Непустота контента обязательна: первый чанк каждого LLM-вызова несёт
        # content='' (role-чанк), а Message.__post_init__ фиксирует created_at
        # в момент создания объекта. Создание ответа на пустом чанке ставит
        # его created_at РАНЬШЕ шагов процесса — и ответ всплывает над
        # контейнером «процесс ответа» и в ленте, и в истории.
        if (
            isinstance(chunk, AIMessageChunk)
            and isinstance(chunk.content, str)
            and chunk.content
        ):
            answer = await _final_message()
            await answer.stream_token(chunk.content)

    # если ответ так и не начался (ни одного content-чанка) — отдаём пустой
    if final_answer is None:
        final_answer = cl.Message(content="")
        final_answer.parent_id = None
    await final_answer.send()


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start():
    pass
    #user = ChainlitAdapter.get_chat_user(cl.user_session)
    # await cl.Message(f"Hello {user}").send()


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

    cb = AgentTracer()
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
