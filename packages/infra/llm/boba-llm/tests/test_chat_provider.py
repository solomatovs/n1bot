"""Стандарт чат-провайдеров: парсер локального ответа, wire openai, мост."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from boba.chat.openai import OpenAiConfig
from boba.chat.provider import (
    ChatDelta,
    ChatProvider,
    ChatProviderError,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    OpenAiChatConfig,
    ToolCallRequest,
    ToolSpec,
)
from boba.llm.bridge import ChatProviderFactory, ProviderChatModel
from boba.llm.local import LocalReplyParser, QwenDialogRender
from boba.llm.openai_chat import OpenAiChatProvider

pytestmark = pytest.mark.anyio


def _feed(parser: LocalReplyParser, text: str, width: int) -> list[ChatDelta]:
    """Скармливает текст кусками фиксированной ширины."""
    deltas: list[ChatDelta] = []
    for start in range(0, len(text), width):
        delta = parser.feed(text[start : start + width])
        if delta is not None:
            deltas.append(delta)

    return deltas


class TestLocalReplyParser:
    """Разбор ответа локальной модели: think, tool_call, произвольная нарезка."""

    REPLY = (
        "<think>\nобдумываю запрос\n</think>\n\n"
        "Начало ответа "
        '<tool_call>\n{"name": "kb_fts_search", '
        '"arguments": {"query": "kerberos", "intent": "ищу"}}\n</tool_call>'
        " конец"
    )

    @pytest.mark.parametrize("width", [1, 3, 7, 1000])
    def test_split_does_not_depend_on_chunking(self, width: int) -> None:
        parser = LocalReplyParser()
        deltas = _feed(parser, self.REPLY, width)
        reply = parser.finish()

        reasoning = "".join(d.reasoning for d in deltas)
        if "обдумываю запрос" not in reasoning:
            raise AssertionError(f"рассуждения из дельт: {reasoning!r}")

        if reply.reasoning.strip() != "обдумываю запрос":
            raise AssertionError(f"рассуждения финала: {reply.reasoning!r}")

        if "Начало ответа" not in reply.content or "конец" not in reply.content:
            raise AssertionError(f"текст финала: {reply.content!r}")

        if "<tool_call>" in reply.content or "<think>" in reply.content:
            raise AssertionError(f"теги не вычищены: {reply.content!r}")

        if len(reply.tool_calls) != 1:
            raise AssertionError(f"вызовы: {reply.tool_calls}")

        call = reply.tool_calls[0]
        if call.name != "kb_fts_search":
            raise AssertionError(call.name)
        if call.arguments != {"query": "kerberos", "intent": "ищу"}:
            raise AssertionError(call.arguments)
        if not call.id:
            raise AssertionError("вызов получил синтетический id")

    def test_malformed_call_stays_in_content(self) -> None:
        parser = LocalReplyParser()
        _feed(parser, "до <tool_call>это не json</tool_call> после", 5)
        reply = parser.finish()

        if reply.tool_calls:
            raise AssertionError(f"битый вызов не вызов: {reply.tool_calls}")

        if "это не json" not in reply.content:
            raise AssertionError(f"битый вызов остался текстом: {reply.content!r}")

    def test_leading_whitespace_is_not_streamed(self) -> None:
        """Пробелы между think и текстом не засоряют стрим ответа."""
        parser = LocalReplyParser()
        deltas = _feed(parser, "<think>x</think>\n\n  ответ", 3)

        streamed = "".join(d.content for d in deltas)
        if streamed != "ответ"[: len(streamed)]:
            raise AssertionError(f"стрим начался с текста: {streamed!r}")

    def test_plain_text_passes_through(self) -> None:
        parser = LocalReplyParser()
        deltas = _feed(parser, "просто ответ без тегов", 4)
        reply = parser.finish()

        if reply.content != "просто ответ без тегов":
            raise AssertionError(reply.content)
        if reply.reasoning or reply.tool_calls:
            raise AssertionError("ни рассуждений, ни вызовов")
        if (
            "".join(d.content for d in deltas)
            != reply.content[: len("".join(d.content for d in deltas))]
        ):
            raise AssertionError("дельты — префикс финала")


class TestQwenDialogRender:
    """Сборка json-диалога: tools в system, роли и вызовы в сообщениях."""

    TOOLS = (
        ToolSpec(
            name="kb_fts_search",
            description="Поиск.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    )

    def test_tools_merge_into_system_with_full_schema(self) -> None:
        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.SYSTEM, content="Ты ассистент"),
                ChatTurn(role=ChatRole.USER, content="вопрос"),
            ],
            tools=self.TOOLS,
        )

        turns = json.loads(QwenDialogRender.messages_json(request))

        if len(turns) != 2:
            raise AssertionError(f"turns: {turns}")

        system = turns[0]["content"]
        if "Ты ассистент" not in system:
            raise AssertionError("промпт профиля сохранён")
        if '"required": ["query"]' not in system:
            raise AssertionError(f"схема аргументов полная: {system}")
        if "<tools>" not in system or "</tools>" not in system:
            raise AssertionError("блок tools отрендерен")

    def test_assistant_calls_and_tool_role(self) -> None:
        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.USER, content="вопрос"),
                ChatTurn(
                    role=ChatRole.ASSISTANT,
                    tool_calls=[
                        ToolCallRequest(
                            id="c1",
                            name="kb_fts_search",
                            arguments={"query": "kerberos"},
                        )
                    ],
                ),
                ChatTurn(role=ChatRole.TOOL, content="found", tool_call_id="c1"),
            ],
        )

        turns = json.loads(QwenDialogRender.messages_json(request))

        assistant = turns[1]
        calls = assistant["tool_calls"]
        if calls[0]["function"]["name"] != "kb_fts_search":
            raise AssertionError(calls)
        if calls[0]["function"]["arguments"] != {"query": "kerberos"}:
            raise AssertionError(calls)

        if turns[2]["role"] != "tool":
            raise AssertionError(turns[2])


def _sse(*chunks: dict[str, Any]) -> bytes:
    lines: list[str] = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")

    return "".join(lines).encode()


def _delta_chunk(delta: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"delta": delta}]}


def _provider(handler) -> OpenAiChatProvider:
    cfg = OpenAiChatConfig(
        provider="openai",
        openai=OpenAiConfig(base_url="https://fake/v1", api_key="k"),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return OpenAiChatProvider(cfg, client, "test-model")


async def _events(provider: ChatProvider, request: ChatRequest) -> list:
    events = []
    async for event in provider.chat(request):
        events.append(event)

    return events


REQUEST = ChatRequest(messages=[ChatTurn(role=ChatRole.USER, content="hi")])


class TestSseGrammar:
    """Поток разбирается по грамматике SSE, а не по префиксу `data: `."""

    async def test_no_space_multiline_data_comments_and_event_names(self) -> None:
        first = json.dumps(_delta_chunk({"content": "от"}))
        head = '{"choices": [{"delta": '
        tail = '{"content": "вет"}}]}'
        body = (
            ": keepalive\n\n"
            f"data:{first}\n\n"
            f"event: chunk\ndata: {head}\ndata: {tail}\n\n"
            "id: 7\ndata: [DONE]\n\n"
        ).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        events = await _events(_provider(handler), REQUEST)

        reply = events[-1]
        if not isinstance(reply, ChatReply):
            raise AssertionError("финал потока — ChatReply")
        if reply.content != "ответ":
            raise AssertionError(reply.content)

    async def test_event_without_trailing_blank_line_is_delivered(self) -> None:
        body = f"data: {json.dumps(_delta_chunk({'content': 'x'}))}\n".encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        events = await _events(_provider(handler), REQUEST)

        reply = events[-1]
        if not isinstance(reply, ChatReply) or reply.content != "x":
            raise AssertionError(events)


class TestOpenAiChatProvider:
    """Wire-формат: SSE-дельты, склейка вызовов, usage, повторы, не-стрим."""

    async def test_stream_deltas_and_final_reply(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk({"reasoning_content": "думаю"}),
                _delta_chunk({"content": "от"}),
                _delta_chunk({"content": "вет"}),
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "probe", "arguments": '{"q":'},
                            }
                        ]
                    }
                ),
                _delta_chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": ' "x"}'}}]}
                ),
                {
                    "choices": [{"delta": {}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            )
            return httpx.Response(200, content=body)

        events = await _events(_provider(handler), REQUEST)

        reply = events[-1]
        if not isinstance(reply, ChatReply):
            raise AssertionError("финал потока — ChatReply")

        if reply.content != "ответ" or reply.reasoning != "думаю":
            raise AssertionError(f"{reply.content!r} {reply.reasoning!r}")

        if reply.tool_calls[0].arguments != {"q": "x"}:
            raise AssertionError(f"вызов склеен: {reply.tool_calls}")
        if reply.tool_calls[0].id != "call-1":
            raise AssertionError(reply.tool_calls)

        if reply.usage.input_tokens != 11 or reply.usage.output_tokens != 7:
            raise AssertionError(f"usage: {reply.usage}")

        deltas = [e for e in events if isinstance(e, ChatDelta)]
        if "".join(d.content for d in deltas) != "ответ":
            raise AssertionError(f"дельты: {deltas}")

    async def test_non_stream_request_parses_message_body(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            body = {
                "choices": [
                    {"message": {"role": "assistant", "content": "весь ответ"}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
            return httpx.Response(200, json=body)

        request = REQUEST.model_copy(
            update={"stream": False, "sampling": {"max_completion_tokens": 77}}
        )
        events = await _events(_provider(handler), request)

        if seen[0]["stream"] is not False:
            raise AssertionError("не-стрим просит stream=false")

        # потолок ответа уходит полем нового API, как слал прежний стек
        if seen[0].get("max_completion_tokens") != 77:
            raise AssertionError(f"потолок токенов: {seen[0]}")
        if "max_tokens" in seen[0]:
            raise AssertionError("устаревшее имя поля не отправляется")

        if len(events) != 1:
            raise AssertionError(f"только финал: {events}")

        reply = events[0]
        if reply.content != "весь ответ" or reply.usage.input_tokens != 3:
            raise AssertionError(f"{reply}")

    async def test_retry_before_first_byte(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503)

            return httpx.Response(200, content=_sse(_delta_chunk({"content": "ok"})))

        events = await _events(_provider(handler), REQUEST)

        if len(calls) != 2:
            raise AssertionError(f"повтор состоялся: {len(calls)}")
        if events[-1].content != "ok":
            raise AssertionError(events)

    async def test_client_error_is_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"denied")

        with pytest.raises(ChatProviderError, match="401"):
            await _events(_provider(handler), REQUEST)


class FakeChatProvider(ChatProvider):
    """Провайдер по сценарию: отдаёт заготовленные события."""

    def __init__(self, events: Sequence[object]) -> None:
        self.events = list(events)
        self.requests: list[ChatRequest] = []

    async def chat(self, request: ChatRequest):
        self.requests.append(request)
        for event in self.events:
            yield event


class TestProviderChatModel:
    """Мост: конверсия сообщений, чанки стрима, финал с вызовами."""

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
        provider = FakeChatProvider([ChatReply(content="ответ")])
        chat = ProviderChatModel(provider=provider)

        await chat.ainvoke(self.MESSAGES)

        turns = provider.requests[0].messages
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
        events = [
            ChatDelta(reasoning="ду"),
            ChatDelta(content="от"),
            ChatReply(
                content="от",
                tool_calls=[
                    ToolCallRequest(id="c9", name="probe", arguments={"q": "y"})
                ],
            ),
        ]
        chat = ProviderChatModel(provider=FakeChatProvider(events))

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
        provider = FakeChatProvider([ChatReply(content="ответ")])
        chat = ProviderChatModel(provider=provider)

        answer = await chat.ainvoke([HumanMessage("hi")])

        if provider.requests[0].stream is not False:
            raise AssertionError("ainvoke идёт без стрима")
        if answer.content != "ответ":
            raise AssertionError(answer)

    async def test_bound_tools_reach_the_request(self) -> None:
        provider = FakeChatProvider([ChatReply(content="ok")])
        chat = ProviderChatModel(provider=provider)

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

        tools = provider.requests[0].tools
        if len(tools) != 1 or tools[0].name != "probe":
            raise AssertionError(tools)


class TestChatProviderFactory:
    """Фабрика: реализация по конфигу, недостающий ресурс — отказ."""

    def test_openai_needs_client(self) -> None:
        cfg = OpenAiChatConfig(
            provider="openai",
            openai=OpenAiConfig(base_url="https://x/v1", api_key="k"),
        )

        with pytest.raises(ValueError, match="httpx client"):
            ChatProviderFactory.build(cfg, model="m", client=None, runtime=None)

    def test_openai_builds_provider(self) -> None:
        cfg = OpenAiChatConfig(
            provider="openai",
            openai=OpenAiConfig(base_url="https://x/v1", api_key="k"),
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )

        built = ChatProviderFactory.build(cfg, model="m", client=client, runtime=None)
        if not isinstance(built, OpenAiChatProvider):
            raise AssertionError(type(built))


class TestReasoningRoundTrip:
    """Рассуждения возвращаются провайдеру: пустые — тоже, отсутствующие — нет."""

    async def test_empty_reasoning_is_sent_back(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, content=_sse(_delta_chunk({"content": "ok"})))

        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.USER, content="q"),
                ChatTurn(role=ChatRole.ASSISTANT, content="", reasoning=""),
                ChatTurn(role=ChatRole.USER, content="more"),
            ]
        )
        await _events(_provider(handler), request)

        assistant = seen[0]["messages"][1]
        if "reasoning_content" not in assistant:
            raise AssertionError(f"пустые рассуждения не вернулись: {assistant}")
        if assistant["reasoning_content"] != "":
            raise AssertionError(assistant)

    async def test_absent_reasoning_is_not_sent(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, content=_sse(_delta_chunk({"content": "ok"})))

        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.USER, content="q"),
                ChatTurn(role=ChatRole.ASSISTANT, content="a"),
            ]
        )
        await _events(_provider(handler), request)

        assistant = seen[0]["messages"][1]
        if "reasoning_content" in assistant:
            raise AssertionError(f"поле без ключа не отправляется: {assistant}")

    async def test_bridge_keeps_the_empty_key(self) -> None:
        provider = FakeChatProvider([ChatReply(content="ok")])
        chat = ProviderChatModel(provider=provider)

        await chat.ainvoke(
            [
                HumanMessage("q"),
                AIMessage(content="", additional_kwargs={"reasoning_content": ""}),
                AIMessage(content="plain"),
            ]
        )

        turns = provider.requests[0].messages
        if turns[1].reasoning != "":
            raise AssertionError(f"пустой ключ сохранён: {turns[1]}")
        if turns[2].reasoning is not None:
            raise AssertionError(f"без ключа — None: {turns[2]}")
