"""Стык графа хода с портом инструментов.

Ядро запуска (boba.toolrun) и порт ToolServer нейтральны: карточка
инструмента, вызов-узел DagNode, итог NodeOutcome. Граф хода собран
create_agent langchain: модели он отдаёт инструменты BaseTool, вызовы ответа
раздаёт по одному запросами ToolCallRequest и ждёт сообщения ToolMessage.
LangchainPort — единственное место, где вызовы инструментов чата говорят
типами графа: заглушки инструментов по карточкам порта и ответ на запрос
вызова. Исполняет вызовы порт; langchain в исполнении не участвует.

Ошибки:
RuntimeError — тело заглушки инструмента вызвано мимо порта; порт принял
    пакет без вызова, который ждёт ответа.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar, Literal

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from boba.chainlit.agent.bridge import LangchainMessages
from boba.toolkit.chain import StreamFailureKind
from boba.toolkit.dag import DagNode, NodeOutcome, NodeOutcomes, ToolCard, ToolServer
from boba.toolkit.result import ErrorResult, ToolResultBase

__all__ = ["LangchainPort"]


class LangchainPort:
    """Порт ToolServer глазами графа хода.

    Создаётся билдером графа поверх порта сессии. Графу отдаёт заглушки
    инструментов по карточкам порта: create_agent показывает их модели и
    находит по ним вызовы; тела заглушек не исполняются — вызовы забирает
    middleware (ServerCallMiddleware) и шлёт сюда. ToolNode раздаёт вызовы
    ответа модели по одному и одновременно, а порт принимает их пакетом
    ответа. Первый пришедший вызов ответа отправляет порту весь пакет
    узлами DagNode, остальные берут из того же пакета своё ожидание; каждый
    вызов отвечает сообщением ToolMessage, как только готов сам.
    """

    RESPONSE_FORMAT: ClassVar[Literal["content_and_artifact"]] = "content_and_artifact"

    def __init__(self, service: ToolServer) -> None:
        self._service = service
        self._messages = LangchainMessages()
        self._outcomes = NodeOutcomes()
        self._batches: dict[
            str, asyncio.Future[Sequence[asyncio.Future[NodeOutcome]]]
        ] = {}
        self._waiting: dict[str, set[str]] = {}

    def tools(self) -> list[BaseTool]:
        """Заглушки инструментов порта для графа и модели."""
        offered: list[BaseTool] = []
        for card in self._service.tools():
            offered.append(self._stub(card))

        return offered

    async def reply(self, request: ToolCallRequest) -> ToolMessage:
        """Сообщение инструмента на вызов request из ответа модели. Отмена
        ожидания останавливает вызов."""
        calls = self._response_calls(request)
        call_id = str(request.tool_call["id"])
        batch_id = str(calls[0]["id"])

        batch = self._batches.get(batch_id)
        if batch is None:
            batch = asyncio.ensure_future(self._submitted(calls))
            self._batches[batch_id] = batch
            self._waiting[batch_id] = self._ids_of(calls)

        pending = await batch
        own = self._own(calls, pending, call_id)

        try:
            return self._messages.tool_message(await own)
        except asyncio.CancelledError:
            own.cancel()
            raise
        finally:
            self._taken(batch_id, call_id)

    async def _submitted(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        """Вызовы одного ответа модели уходят порту одним пакетом; итог
        каждого — своё ожидание, в порядке вызовов. Вызов без идентификатора
        порту не назвать: он получает готовый отказ."""
        pending: dict[int, asyncio.Future[NodeOutcome]] = {}
        nodes: list[DagNode] = []
        positions: list[int] = []
        for position, call in enumerate(calls):
            if not call["id"]:
                pending[position] = self._outcomes.settled(self._unnamed(call))
                continue

            nodes.append(self._node(call))
            positions.append(position)

        accepted = await self._service.submit(nodes)
        for position, future in zip(positions, accepted, strict=True):
            pending[position] = future

        ordered: list[asyncio.Future[NodeOutcome]] = []
        for position in range(len(calls)):
            ordered.append(pending[position])

        return ordered

    def _taken(self, batch_id: str, call_id: str) -> None:
        """Вызов забрал свой итог; пакет забывается, когда забрали все."""
        waiting = self._waiting.get(batch_id)
        if waiting is None:
            return

        waiting.discard(call_id)
        if waiting:
            return

        self._batches.pop(batch_id, None)
        self._waiting.pop(batch_id, None)

    @staticmethod
    def _own(
        calls: Sequence[ToolCall],
        pending: Sequence[asyncio.Future[NodeOutcome]],
        call_id: str,
    ) -> asyncio.Future[NodeOutcome]:
        for call, future in zip(calls, pending, strict=True):
            if str(call["id"]) == call_id:
                return future

        msg = f"tool server accepted no call with id {call_id!r}"
        raise RuntimeError(msg)

    @staticmethod
    def _ids_of(calls: Sequence[ToolCall]) -> set[str]:
        ids: set[str] = set()
        for call in calls:
            ids.add(str(call["id"]))

        return ids

    @staticmethod
    def _response_calls(request: ToolCallRequest) -> Sequence[ToolCall]:
        """Все вызовы ответа модели, которому принадлежит вызов request."""
        own = request.tool_call
        for message in reversed(request.state["messages"]):
            if not isinstance(message, AIMessage):
                continue

            for call in message.tool_calls:
                if call["id"] == own["id"]:
                    return message.tool_calls

        return [own]

    @staticmethod
    def _node(call: ToolCall) -> DagNode:
        return DagNode(key=str(call["id"]), tool=call["name"], args=call["args"])

    def _unnamed(self, call: ToolCall) -> NodeOutcome:
        """Отказ вызову без идентификатора."""
        name = call["name"]
        failure = ErrorResult(
            message=f"dag plan: call of {name!r} has no tool call id",
            error_kind=StreamFailureKind.PLAN_REFUSED,
        )

        return self._outcomes.unnamed(str(call["id"]), name, failure)

    def _stub(self, card: ToolCard) -> StructuredTool:
        """Заглушка инструмента: схема аргументов — готовая JSON-схема
        карточки; langchain её не проверяет, проверка одна — у исполнителя."""
        return StructuredTool(
            name=card.name,
            description=card.description,
            args_schema=dict(card.parameters),
            coroutine=self._bypassed,
            response_format=self.RESPONSE_FORMAT,
        )

    @staticmethod
    async def _bypassed(**arguments: object) -> tuple[str, ToolResultBase]:
        msg = (
            "a tool of the agent graph is executed by the tool server through "
            "the call middleware; its body must not be called"
        )
        raise RuntimeError(msg)
