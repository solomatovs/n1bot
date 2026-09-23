"""Мост ChatModel -> langchain: конверсия сообщений, чанки стрима, финал с вызовами."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from boba.chainlit.agent.bridge import ChatModelBridge
from boba.llm.chat import (
    ChatDelta,
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ToolCall,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: мост в контекст chainlit не ходит."""


class ScriptedChatModel(ChatModel):
    """Модель по сценарию: отдаёт заготовленные события, запоминает запросы."""

    def __init__(self, events: Sequence[ChatEvent]) -> None:
        self.events = list(events)
        self.requests: list[ChatRequest] = []

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        self.requests.append(request)
        for event in self.events:
            yield event


class TestChatModelBridge:
    MESSAGES = [
        SystemMessage("prompt"),
        HumanMessage("вопрос"),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "думал"},
            tool_calls=[
                {"name": "probe", "args": {"q": "x"}, "id": "c1", "type": "tool_call"}
            ],
        ),
        ToolMessage(content="found", tool_call_id="c1"),
    ]

    async def test_messages_convert_to_turns(self) -> None:
        model = ScriptedChatModel([ChatReply(content="ответ")])
        chat = ChatModelBridge(chat_model=model)

        await chat.ainvoke(self.MESSAGES)

        turns = model.requests[0].messages
        roles = [turn.role for turn in turns]
        if roles != [ChatRole.SYSTEM, ChatRole.USER, ChatRole.ASSISTANT, ChatRole.TOOL]:
            raise AssertionError(roles)

        assistant = turns[2]
        if assistant.reasoning != "думал":
            raise AssertionError(assistant)
        if assistant.tool_calls[0].arguments != {"q": "x"}:
            raise AssertionError(assistant.tool_calls)

        if turns[3].tool_call_id != "c1":
            raise AssertionError(turns[3])

    async def test_stream_yields_tool_calls_in_final_chunk(self) -> None:
        events: list[ChatEvent] = [
            ChatDelta(reasoning="ду"),
            ChatDelta(content="от"),
            ChatReply(
                content="от",
                tool_calls=[ToolCall(id="c9", name="probe", arguments={"q": "y"})],
            ),
        ]
        chat = ChatModelBridge(chat_model=ScriptedChatModel(events))

        merged: AIMessageChunk | None = None
        async for chunk in chat.astream([HumanMessage("hi")]):
            if not isinstance(chunk, AIMessageChunk):
                raise AssertionError(f"чанк моста: {type(chunk)}")

            if merged is None:
                merged = chunk
            else:
                merged = merged + chunk

        if merged is None:
            raise AssertionError("стрим отдал чанки")

        if merged.content != "от":
            raise AssertionError(f"контент из дельт: {merged.content!r}")

        if merged.additional_kwargs.get("reasoning_content") != "ду":
            raise AssertionError(merged.additional_kwargs)

        calls = merged.tool_calls
        if len(calls) != 1 or calls[0]["args"] != {"q": "y"}:
            raise AssertionError(calls)
        if calls[0]["id"] != "c9":
            raise AssertionError(calls)

    async def test_ainvoke_asks_for_non_stream(self) -> None:
        model = ScriptedChatModel([ChatReply(content="ответ")])
        chat = ChatModelBridge(chat_model=model)

        answer = await chat.ainvoke([HumanMessage("hi")])

        if model.requests[0].stream is not False:
            raise AssertionError("ainvoke идёт без стрима")
        if answer.content != "ответ":
            raise AssertionError(answer)

    async def test_bound_tools_reach_the_request(self) -> None:
        model = ScriptedChatModel([ChatReply(content="ok")])
        chat = ChatModelBridge(chat_model=model)

        declared = {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "Проба.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        bound = chat.bind_tools([declared])
        await bound.ainvoke([HumanMessage("hi")])

        tools = model.requests[0].tools
        if len(tools) != 1 or tools[0].name != "probe":
            raise AssertionError(tools)
