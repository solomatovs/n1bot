"""Callback'и chainlit: мост между интерфейсом чата и агентом langgraph.

Здесь живут точки входа chainlit (сообщение, старт/конец чата, восстановление
треда, data layer). Отрисовку ленты ведёт ChatView, историю — checkpointer.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

from fastapi import Request, Response
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.chat.edit import ThreadRewind
from boba.chainlit.chat.handler import chainlit_error_ctx_handler
from boba.chainlit.infra.di import Depends, di_inject
from boba.chainlit.infra.providers import chainlit_data_layer, langchain_agent
from boba.chainlit.infra.session import current_user_id
from boba.chainlit.rendering.chat_view import ChatView, LiveSink
from boba.tool.shell import WORKSPACE_MOUNT
from boba.toolkit.cancellation import turn_cancellation
from chainlit.data.base import BaseDataLayer
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.types import ThreadDict
from chainlit.user_session import UserSession

logger = logging.getLogger(__name__)


class ChainlitAdapter:
    "Мост взаимодействия бизнес логики и chainlit"

    @staticmethod
    def get_chat_id(session: HTTPSession | WebsocketSession):
        return session.thread_id

    @staticmethod
    def to_human_message(msg: cl.Message) -> HumanMessage:
        """Сообщение пользователя; пути вложений — как их видит песочница."""
        attachments: list[dict[str, str]] = []
        for element in msg.elements or []:
            key = ObjectKey.build(
                current_user_id(), element.thread_id, element.name, element.id
            )
            attachments.append(
                {
                    "name": element.name or element.id,
                    "path": f"{WORKSPACE_MOUNT}/{key.in_thread()}",
                }
            )
        extra = {"attachments": attachments} if attachments else {}
        return HumanMessage(content=msg.content, id=msg.id, additional_kwargs=extra)

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

    @staticmethod
    async def report_stop(
        graph: CompiledStateGraph,
        thread_id: str,
        tracer: AgentTracer,
        key: str,
        answer: cl.Message | None,
    ) -> None:
        "фиксирует остановку: закрывает шаги и кладёт прерванный ответ в историю"
        await tracer.stop_pending()

        partial = (answer.content if answer else "") or ""
        note = f"_{ChatView.STOPPED_TEXT}_"
        content = f"{partial}\n\n{note}" if partial else note

        if answer is not None:
            answer.content = content
            await answer.send()
        else:
            await tracer.view.answer(content, key)

        await graph.aupdate_state(
            RunnableConfig(configurable={"thread_id": thread_id}),
            {
                "messages": [
                    AIMessage(content=content, additional_kwargs={"stopped": True})
                ]
            },
        )

    @staticmethod
    async def refresh_view(data_layer: BaseDataLayer, thread_id: str) -> None:
        "перерисовывает ленту треда из истории агента"
        if thread := await data_layer.get_thread(thread_id):
            await cl.context.emitter.resume_thread(thread)


@cl.on_message
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
    thread_id = ChainlitAdapter.get_chat_id(cl.context.session)

    rewind = ThreadRewind(graph, data_layer, thread_id)
    if await rewind.is_edit(msg.id):
        await rewind.apply(msg.id, msg.content)
        await ChainlitAdapter.refresh_view(data_layer, thread_id)

    view = ChatView(thread_id, LiveSink())
    tracer = AgentTracer(view)
    run_config = RunnableConfig(
        callbacks=[tracer],
        configurable={"thread_id": thread_id},
    )

    stream = cast(
        "AsyncIterator[tuple[BaseMessage, dict[str, Any]]]",
        graph.astream(
            {"messages": [ChainlitAdapter.to_human_message(msg)]},
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

    with turn_cancellation() as cancellation:
        try:
            async for chunk, _metadata in stream:
                if (
                    isinstance(chunk, AIMessageChunk)
                    and isinstance(chunk.content, str)
                    and chunk.content
                ):
                    answer = await _final_message()
                    await answer.stream_token(chunk.content)
        except asyncio.CancelledError:
            cancellation.cancel()
            await ChainlitAdapter.report_stop(
                graph, thread_id, tracer, msg.id, final_answer
            )
            raise
        except Exception as e:
            logger.exception("agent run failed")
            cancellation.cancel()
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
