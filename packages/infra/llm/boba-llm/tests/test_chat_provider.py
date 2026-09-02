"""Стандарт чат-провайдеров: парсер локального ответа, wire openai и ollama, мост."""

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

from boba.chat.http import HttpConfig
from boba.chat.provider import (
    ChatDelta,
    ChatProvider,
    ChatProviderError,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    OllamaChatConfig,
    OpenAiChatConfig,
    ToolCallRequest,
    ToolSpec,
)
from boba.llm.bridge import ChatProviderFactory, ProviderChatModel
from boba.llm.local import (
    LocalReplyParser,
    OnnxChatRuntime,
    OnnxGenai,
    OnnxGenerator,
    OnnxModel,
    OnnxParams,
    OnnxTokenizer,
    OnnxTokenStream,
    QwenDialogRender,
    RunSpec,
)
from boba.llm.ollama_chat import OllamaChatProvider
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


class _FakeModel(OnnxModel):
    """Пустышка загруженной модели."""


class _FakeStream(OnnxTokenStream):
    def decode(self, token: int) -> str:
        return "x"


class _FakeTokenizer(OnnxTokenizer):
    def encode(self, text: str) -> Sequence[int]:
        return [1, 2, 3]

    def decode(self, tokens: Sequence[int]) -> str:
        return "x" * len(tokens)

    def create_stream(self) -> OnnxTokenStream:
        return _FakeStream()

    def apply_chat_template(
        self, messages: str, *, add_generation_prompt: bool
    ) -> str:
        return messages


class _FakeParams(OnnxParams):
    def __init__(self) -> None:
        self.max_length = 0

    def set_search_options(self, **options: object) -> None:
        raw = options["max_length"]
        assert isinstance(raw, int)
        self.max_length = raw

    def set_guidance(self, kind: str, data: str) -> None:
        return None


class _FakeGenerator(OnnxGenerator):
    """Генерация до EOS либо до max_length — как настоящий рантайм."""

    def __init__(self, max_length: int, eos_after: int | None) -> None:
        self._max_length = max_length
        self._eos_after = eos_after
        self._held = 0
        self._produced = 0

    def append_tokens(self, tokens: Sequence[int]) -> None:
        self._held += len(tokens)

    def is_done(self) -> bool:
        if self._eos_after is not None and self._produced >= self._eos_after:
            return True

        return self._held >= self._max_length

    def generate_next_token(self) -> None:
        self._held += 1
        self._produced += 1

    def get_next_tokens(self) -> Sequence[int]:
        return [42]

    def get_sequence(self, index: int) -> Sequence[int]:
        return []


class _FakeGenai(OnnxGenai):
    """Рантайм без onnxruntime_genai: генерация по правилам фейка."""

    def __init__(self, eos_after: int | None = None) -> None:
        self._eos_after = eos_after

    def load(self, model_dir: str) -> tuple[OnnxModel, OnnxTokenizer]:
        return _FakeModel(), _FakeTokenizer()

    def params(self, model: OnnxModel) -> OnnxParams:
        return _FakeParams()

    def generator(self, model: OnnxModel, params: OnnxParams) -> OnnxGenerator:
        assert isinstance(params, _FakeParams)
        return _FakeGenerator(params.max_length, self._eos_after)


class TestLocalTokenCeiling:
    """Полный расход max_tokens локального прогона — честная ошибка."""

    def test_hitting_the_ceiling_raises(self) -> None:
        runtime = OnnxChatRuntime("fake-model", _FakeGenai())
        pieces: list[str] = []

        with pytest.raises(ChatProviderError, match="hit the token ceiling"):
            runtime.run(
                "prompt",
                RunSpec(max_tokens=5),
                pieces.append,
                lambda: False,
            )

        if len(pieces) != 5:
            raise AssertionError(pieces)

    def test_eos_before_the_ceiling_is_fine(self) -> None:
        runtime = OnnxChatRuntime("fake-model", _FakeGenai(eos_after=2))
        pieces: list[str] = []

        runtime.run("prompt", RunSpec(max_tokens=5), pieces.append, lambda: False)

        if len(pieces) != 2:
            raise AssertionError(pieces)


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
        kind="openai",
        http=HttpConfig(),
        base_url="https://fake/v1",
        api_key="k",
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

    async def test_content_filter_finish_is_an_honest_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk({"content": "нач"}),
                {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="content filter"):
            await _events(_provider(handler), REQUEST)

    async def test_unknown_finish_reason_is_an_honest_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk({"content": "нач"}),
                {
                    "choices": [
                        {
                            "delta": {},
                            "finish_reason": "insufficient_system_resource",
                        }
                    ]
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(
            ChatProviderError, match="insufficient_system_resource"
        ):
            await _events(_provider(handler), REQUEST)

    async def test_stop_and_tool_calls_finishes_are_complete(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "probe", "arguments": "{}"},
                            }
                        ]
                    }
                ),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            )
            return httpx.Response(200, content=body)

        events = await _events(_provider(handler), REQUEST)

        reply = events[-1]
        if not isinstance(reply, ChatReply):
            raise AssertionError("финал потока — ChatReply")
        if reply.tool_calls[0].name != "probe":
            raise AssertionError(reply.tool_calls)

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
            kind="openai",
            http=HttpConfig(),
            base_url="https://x/v1",
            api_key="k",
        )

        with pytest.raises(ValueError, match="httpx client"):
            ChatProviderFactory.build(cfg, model="m", client=None, runtime=None)

    def test_openai_builds_provider(self) -> None:
        cfg = OpenAiChatConfig(
            kind="openai",
            http=HttpConfig(),
            base_url="https://x/v1",
            api_key="k",
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


def _ndjson(*chunks: dict[str, Any]) -> bytes:
    lines: list[str] = []
    for chunk in chunks:
        lines.append(json.dumps(chunk, ensure_ascii=False) + "\n")

    return "".join(lines).encode()


def _ollama_provider(handler) -> OllamaChatProvider:
    cfg = OllamaChatConfig(
        kind="ollama",
        http=HttpConfig(),
        base_url="http://fake:11434",
        api_key="k",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return OllamaChatProvider(cfg, client, "test-model")


def _ollama_chunk(message: dict[str, Any]) -> dict[str, Any]:
    return {"model": "test-model", "message": message, "done": False}


class TestOllamaChatProvider:
    """Wire нативного /api/chat: NDJSON-дельты, вызовы объектами, usage, тело."""

    async def test_stream_deltas_and_final_reply(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _ndjson(
                _ollama_chunk({"role": "assistant", "thinking": "думаю"}),
                _ollama_chunk({"role": "assistant", "content": "от"}),
                _ollama_chunk({"role": "assistant", "content": "вет"}),
                _ollama_chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "index": 0,
                                    "name": "probe",
                                    "arguments": {"q": "x"},
                                },
                            }
                        ],
                    }
                ),
                {
                    "model": "test-model",
                    "message": {"role": "assistant"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 11,
                    "eval_count": 7,
                },
            )
            return httpx.Response(200, content=body)

        events = await _events(_ollama_provider(handler), REQUEST)

        reply = events[-1]
        if not isinstance(reply, ChatReply):
            raise AssertionError("финал потока — ChatReply")

        if reply.content != "ответ" or reply.reasoning != "думаю":
            raise AssertionError(f"{reply.content!r} {reply.reasoning!r}")

        if reply.tool_calls[0].arguments != {"q": "x"}:
            raise AssertionError(f"аргументы объектом: {reply.tool_calls}")
        if reply.tool_calls[0].id != "call-1":
            raise AssertionError(reply.tool_calls)

        if reply.usage.input_tokens != 11 or reply.usage.output_tokens != 7:
            raise AssertionError(f"usage: {reply.usage}")

        deltas = [e for e in events if isinstance(e, ChatDelta)]
        if "".join(d.content for d in deltas) != "ответ":
            raise AssertionError(f"дельты текста: {deltas}")
        if "".join(d.reasoning for d in deltas) != "думаю":
            raise AssertionError(f"дельты рассуждений: {deltas}")

    async def test_non_stream_request_parses_whole_body(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            body = {
                "model": "test-model",
                "message": {"role": "assistant", "content": "весь ответ"},
                "done": True,
                "prompt_eval_count": 3,
                "eval_count": 2,
            }
            return httpx.Response(200, json=body)

        request = ChatRequest(
            messages=[ChatTurn(role=ChatRole.USER, content="hi")],
            stream=False,
        )
        events = await _events(_ollama_provider(handler), request)

        if len(events) != 1:
            raise AssertionError(f"без потока только финал: {events}")

        reply = events[0]
        if not isinstance(reply, ChatReply):
            raise AssertionError("финал — ChatReply")
        if reply.content != "весь ответ":
            raise AssertionError(reply.content)
        if reply.usage.input_tokens != 3 or reply.usage.output_tokens != 2:
            raise AssertionError(f"usage: {reply.usage}")

        if seen[0]["stream"] is not False:
            raise AssertionError(f"stream в теле: {seen[0]}")

    async def test_payload_sampling_as_is_and_stop_moves_into_options(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            body = {
                "model": "test-model",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
            }
            return httpx.Response(200, json=body)

        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.SYSTEM, content="be brief"),
                ChatTurn(role=ChatRole.USER, content="hi"),
                ChatTurn(
                    role=ChatRole.ASSISTANT,
                    content="",
                    reasoning="прикинул",
                    tool_calls=[
                        ToolCallRequest(id="c1", name="probe", arguments={"q": "x"})
                    ],
                ),
                ChatTurn(role=ChatRole.TOOL, content="found", tool_call_id="c1"),
                ChatTurn(role=ChatRole.USER, content="and?"),
            ],
            tools=[
                ToolSpec(name="probe", description="d", parameters={"type": "object"})
            ],
            sampling={
                "think": "low",
                "options": {"top_k": 20, "num_predict": 4096},
                "stop": ["</s>"],
            },
            stream=False,
        )
        await _events(_ollama_provider(handler), request)

        payload = seen[0]
        if payload["think"] != "low":
            raise AssertionError(f"think верхним уровнем: {payload}")

        if payload["options"] != {"top_k": 20, "num_predict": 4096, "stop": ["</s>"]}:
            raise AssertionError(f"options со stop моста: {payload['options']}")
        if "stop" in payload:
            raise AssertionError(f"stop не должен остаться в корне: {payload}")

        assistant = payload["messages"][2]
        if assistant["thinking"] != "прикинул":
            raise AssertionError(f"thinking ассистента: {assistant}")
        call = assistant["tool_calls"][0]
        if call["function"]["arguments"] != {"q": "x"}:
            raise AssertionError(f"аргументы объектом: {call}")

        tool = payload["messages"][3]
        if tool["tool_call_id"] != "c1":
            raise AssertionError(f"ответ инструмента: {tool}")

        declared = payload["tools"][0]
        if declared["function"]["name"] != "probe":
            raise AssertionError(f"объявление инструмента: {declared}")

    async def test_error_chunk_raises_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _ndjson(
                _ollama_chunk({"role": "assistant", "content": "нач"}),
                {"error": "model runner crashed"},
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="model runner crashed"):
            await _events(_ollama_provider(handler), REQUEST)

    async def test_done_reason_length_is_an_honest_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _ndjson(
                _ollama_chunk({"role": "assistant", "content": "нач"}),
                {
                    "model": "test-model",
                    "message": {"role": "assistant"},
                    "done": True,
                    "done_reason": "length",
                    "prompt_eval_count": 11,
                    "eval_count": 512,
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="hit the token ceiling"):
            await _events(_ollama_provider(handler), REQUEST)

    async def test_unknown_done_reason_is_an_honest_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _ndjson(
                {
                    "model": "test-model",
                    "message": {"role": "assistant"},
                    "done": True,
                    "done_reason": "unload",
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="done_reason=unload"):
            await _events(_ollama_provider(handler), REQUEST)

    async def test_tool_call_without_id_gets_local_one(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _ndjson(
                _ollama_chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"function": {"name": "probe", "arguments": {}}}
                        ],
                    }
                ),
                {"model": "test-model", "message": {"role": "assistant"}, "done": True},
            )
            return httpx.Response(200, content=body)

        events = await _events(_ollama_provider(handler), REQUEST)

        reply = events[-1]
        if not isinstance(reply, ChatReply):
            raise AssertionError("финал — ChatReply")
        if not reply.tool_calls[0].id:
            raise AssertionError("вызов без id от сервера получает локальный")

    async def test_http_error_status_raises_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "model not found"})

        with pytest.raises(ChatProviderError, match="404"):
            await _events(_ollama_provider(handler), REQUEST)

    def test_factory_needs_client(self) -> None:
        cfg = OllamaChatConfig(
            kind="ollama",
            http=HttpConfig(),
            base_url="http://x:11434",
            api_key="k",
        )

        with pytest.raises(ValueError, match="httpx client"):
            ChatProviderFactory.build(cfg, model="m", client=None, runtime=None)

    def test_factory_builds_provider(self) -> None:
        cfg = OllamaChatConfig(
            kind="ollama",
            http=HttpConfig(),
            base_url="http://x:11434",
            api_key="k",
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )

        built = ChatProviderFactory.build(cfg, model="m", client=client, runtime=None)
        if not isinstance(built, OllamaChatProvider):
            raise AssertionError(type(built))


class TestTruncatedReply:
    """Обрыв по потолку токенов: причина названа, а не спрятана за 'мусор'."""

    TRUNCATED = (
        '{"space_keys": ["ARROW", "ASTERIXDB"], "attachments": "*", '
        '"force_update": true, "ocr_language": "rus+eng"'
    )
    """Аргументы вызова, оборванные на середине: закрывающей скобки нет."""

    async def test_length_finish_names_the_ceiling(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "confluence_index_spaces"},
                            }
                        ]
                    }
                ),
                _delta_chunk(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": self.TRUNCATED}}
                        ]
                    }
                ),
                {
                    "choices": [{"delta": {"content": ""}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 4096},
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="token ceiling") as caught:
            await _events(_provider(handler), REQUEST)

        message = str(caught.value)
        if "confluence_index_spaces" not in message:
            raise AssertionError(f"в ошибке нет имени вызова: {message}")
        if "4096" not in message:
            raise AssertionError(f"в ошибке нет истраченных токенов: {message}")
        if "max_tokens" not in message:
            raise AssertionError(f"в ошибке нет подсказки, что делать: {message}")

    async def test_malformed_without_length_stays_malformed(self) -> None:
        """Без обрыва по длине битые аргументы остаются битыми аргументами."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "probe", "arguments": "{not json"},
                            }
                        ]
                    }
                ),
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="malformed call arguments"):
            await _events(_provider(handler), REQUEST)

    async def test_length_without_calls_still_names_the_ceiling(self) -> None:
        """Обрыв по лимиту посреди текста — та же честная ошибка, без имени
        вызова: резать было нечего."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk({"content": "начал и не"}),
                {
                    "choices": [{"delta": {}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 4096},
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(ChatProviderError, match="token ceiling") as caught:
            await _events(_provider(handler), REQUEST)

        if "cut off mid-arguments" in str(caught.value):
            raise AssertionError(f"вызов не резался: {caught.value}")
