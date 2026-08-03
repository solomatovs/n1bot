"""Правка вопроса: откат треда к нему и очистка всего, что шло после.

Chainlit присылает правку в обычный on_message с прежним id сообщения,
поэтому историю агента усекаем сами: RemoveMessage для хвоста и замена
текста вопроса одним обновлением состояния графа.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit.rendering.chat_view import ChatView
from chainlit.data.base import BaseDataLayer

__all__ = ["RewindPlan", "ThreadRewind"]


class RewindPlan:
    """Что убрать при откате: сообщения истории и вложения их шагов."""

    def __init__(
        self,
        remove_ids: Sequence[str],
        element_ids: Sequence[str],
    ) -> None:
        self.remove_ids = list(remove_ids)
        self.element_ids = list(element_ids)

    def __bool__(self) -> bool:
        return bool(self.remove_ids or self.element_ids)


class ThreadRewind:
    """Приводит историю треда к состоянию «сразу после этого вопроса»."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        data_layer: BaseDataLayer,
        thread_id: str,
    ) -> None:
        self._graph = graph
        self._data_layer = data_layer
        self._thread_id = thread_id

    @property
    def _config(self) -> RunnableConfig:
        return RunnableConfig(configurable={"thread_id": self._thread_id})

    async def messages(self) -> list[BaseMessage]:
        """Текущая история треда."""
        state = await self._graph.aget_state(self._config)
        return list(state.values.get("messages", []))

    async def is_edit(self, message_id: str) -> bool:
        """Вопрос с таким id уже в истории — значит пришла правка."""
        return any(
            isinstance(m, HumanMessage) and m.id == message_id
            for m in await self.messages()
        )

    @staticmethod
    def plan(
        messages: Sequence[BaseMessage],
        message_id: str,
        thread_id: str,
    ) -> RewindPlan:
        """Хвост после вопроса: id сообщений и id их вложений."""
        index = next(
            (
                i
                for i, m in enumerate(messages)
                if isinstance(m, HumanMessage) and m.id == message_id
            ),
            None,
        )
        if index is None:
            return RewindPlan([], [])

        tail = messages[index + 1 :]
        remove_ids = [m.id for m in tail if m.id]

        element_ids: list[str] = []
        for message in tail:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls or ():
                element_id = ChatView.derive_id(
                    thread_id, call.get("id"), "element"
                )
                if element_id:
                    element_ids.append(element_id)

        return RewindPlan(remove_ids, element_ids)

    async def apply(self, message_id: str, content: str) -> RewindPlan:
        """Удалить хвост и его вложения, поставить вопросу новый текст."""
        rewind = self.plan(await self.messages(), message_id, self._thread_id)

        for element_id in rewind.element_ids:
            await self._data_layer.delete_element(element_id, self._thread_id)

        updates: list[BaseMessage] = [
            RemoveMessage(id=removed) for removed in rewind.remove_ids
        ]
        updates.append(HumanMessage(content=content, id=message_id))
        await self._graph.aupdate_state(self._config, {"messages": updates})
        return rewind
