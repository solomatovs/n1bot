"""HTTP-провайдеры чата: wire openai (SSE) и ollama (NDJSON) поверх HttpTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from boba.llm.chat import (
    ChatDelta,
    ChatImage,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    LlmError,
    ToolSpec,
)
from boba.llm.http.ollama import OllamaBackend, OllamaProvider
from boba.llm.http.openai import OpenAiBackend, OpenAiProvider
from boba.llm.providers import ChatModelConfig
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import BearerAuth, HttpConnection

pytestmark = pytest.mark.anyio

Handler = Callable[[httpx.Request], httpx.Response]

REQUEST = ChatRequest(messages=[ChatTurn(role=ChatRole.USER, content="hi")])

SCHEMA = ToolSpec(
    name="Answer",
    description="answer by schema",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)


def _patch(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    real_client = httpx.AsyncClient

    def mock_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.AsyncClient", mock_client)


def _connection() -> HttpConnection:
    return HttpConnection(
        host="fake",
        path="/v1",
        auth=BearerAuth(method="bearer", token=SecretStr("k")),
        retry_attempts=2,
        retry_backoff_sec=0,
    )


def _openai(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> ChatModel:
    _patch(monkeypatch, handler)
    provider = OpenAiProvider(
        kind="openai", connection=_connection(), transport=HttpTransportConfig()
    )

    return OpenAiBackend(provider).chat(
        ChatModelConfig(provider=provider, model="test-model")
    )


def _ollama(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> ChatModel:
    _patch(monkeypatch, handler)
    provider = OllamaProvider(
        kind="ollama", connection=_connection(), transport=HttpTransportConfig()
    )

    return OllamaBackend(provider).chat(
        ChatModelConfig(provider=provider, model="test-model")
    )


def _sse(*chunks: dict[str, Any]) -> bytes:
    lines: list[str] = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")

    return "".join(lines).encode()


def _delta_chunk(delta: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"delta": delta}]}


async def _events(model: ChatModel, request: ChatRequest) -> list[Any]:
    events: list[Any] = []
    async for event in model.chat(request):
        events.append(event)

    return events


def _reply_of(events: list[Any]) -> ChatReply:
    reply = events[-1]
    if not isinstance(reply, ChatReply):
        raise AssertionError("финал потока — ChatReply")

    return reply


class TestSseGrammar:
    """Поток разбирается по грамматике SSE, а не по префиксу `data: `."""

    async def test_no_space_multiline_data_comments_and_event_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

        reply = _reply_of(await _events(_openai(monkeypatch, handler), REQUEST))
        if reply.content != "ответ":
            raise AssertionError(reply.content)

    async def test_event_without_trailing_blank_line_is_delivered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = f"data: {json.dumps(_delta_chunk({'content': 'x'}))}\n".encode()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        reply = _reply_of(await _events(_openai(monkeypatch, handler), REQUEST))
        if reply.content != "x":
            raise AssertionError(reply)


class TestOpenAiChatModel:
    """Wire-формат: SSE-дельты, склейка вызовов, usage, повторы, не-стрим."""

    async def test_stream_deltas_and_final_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            if str(request.url) != "https://fake/v1/chat/completions":
                return httpx.Response(404, content=str(request.url).encode())

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

        events = await _events(_openai(monkeypatch, handler), REQUEST)

        reply = _reply_of(events)
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

        if seen[0]["authorization"] != "Bearer k":
            raise AssertionError("ключ провайдера уходит из auth соединения")

    async def test_null_delta_fields_are_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """llama.cpp шлёт role-чанк с content: null и null в function."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                {
                    "choices": [
                        {
                            "finish_reason": None,
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": None,
                                "tool_calls": None,
                            },
                        }
                    ],
                    "model": "qwen",
                },
                _delta_chunk({"content": "ok"}),
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": None, "arguments": None},
                            }
                        ]
                    }
                ),
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "probe", "arguments": "{}"},
                            }
                        ]
                    }
                ),
            )
            return httpx.Response(200, content=body)

        reply = _reply_of(await _events(_openai(monkeypatch, handler), REQUEST))
        if reply.content != "ok":
            raise AssertionError(reply.content)
        if reply.tool_calls[0].name != "probe" or reply.tool_calls[0].arguments != {}:
            raise AssertionError(reply.tool_calls)

    async def test_content_filter_finish_is_an_honest_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk({"content": "нач"}),
                {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
            )
            return httpx.Response(200, content=body)

        with pytest.raises(LlmError, match="content filter"):
            await _events(_openai(monkeypatch, handler), REQUEST)

    async def test_unknown_finish_reason_is_an_honest_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse(
                _delta_chunk({"content": "нач"}),
                {
                    "choices": [
                        {"delta": {}, "finish_reason": "insufficient_system_resource"}
                    ]
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(LlmError, match="insufficient_system_resource"):
            await _events(_openai(monkeypatch, handler), REQUEST)

    async def test_stop_and_tool_calls_finishes_are_complete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

        reply = _reply_of(await _events(_openai(monkeypatch, handler), REQUEST))
        if reply.tool_calls[0].name != "probe":
            raise AssertionError(reply.tool_calls)

    async def test_non_stream_request_parses_message_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        events = await _events(_openai(monkeypatch, handler), request)

        if seen[0]["stream"] is not False:
            raise AssertionError("не-стрим просит stream=false")

        # админская таблица сэмплинга уходит в тело как есть
        if seen[0].get("max_completion_tokens") != 77:
            raise AssertionError(f"потолок токенов: {seen[0]}")
        if "max_tokens" in seen[0]:
            raise AssertionError("чужое имя поля не отправляется")

        if len(events) != 1:
            raise AssertionError(f"только финал: {events}")

        reply = _reply_of(events)
        if reply.content != "весь ответ" or reply.usage.input_tokens != 3:
            raise AssertionError(f"{reply}")

    async def test_reply_schema_goes_as_function_with_tool_choice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            body = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "Answer",
                                        "arguments": '{"text": "ok"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body)

        request = REQUEST.model_copy(update={"reply_schema": SCHEMA, "stream": False})
        reply = await _openai(monkeypatch, handler).reply(request)

        sent = seen[0]
        if sent["tools"][0]["function"]["name"] != "Answer":
            raise AssertionError(f"схема ушла функцией: {sent}")
        if sent["tool_choice"] != {"type": "function", "function": {"name": "Answer"}}:
            raise AssertionError(f"tool_choice навязан: {sent}")
        if reply.tool_calls[0].arguments != {"text": "ok"}:
            raise AssertionError(reply)

    async def test_images_go_as_content_parts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            body = {"choices": [{"message": {"role": "assistant", "content": "text"}}]}
            return httpx.Response(200, json=body)

        turn = ChatTurn(
            role=ChatRole.USER,
            content="what is here",
            images=[ChatImage(media_type="image/png", data=b"\x89PNG")],
        )
        request = ChatRequest(messages=[turn], stream=False)
        await _openai(monkeypatch, handler).reply(request)

        parts = seen[0]["messages"][0]["content"]
        if parts[0] != {"type": "text", "text": "what is here"}:
            raise AssertionError(parts)
        if not parts[1]["image_url"]["url"].startswith("data:image/png;base64,"):
            raise AssertionError(parts)

    async def test_retry_before_first_byte(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503)

            return httpx.Response(200, content=_sse(_delta_chunk({"content": "ok"})))

        reply = _reply_of(await _events(_openai(monkeypatch, handler), REQUEST))

        if len(calls) != 2:
            raise AssertionError(f"повтор состоялся: {len(calls)}")
        if reply.content != "ok":
            raise AssertionError(reply)

    async def test_client_error_carries_status_and_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"denied")

        with pytest.raises(LlmError, match=r"401.*denied"):
            await _events(_openai(monkeypatch, handler), REQUEST)


def _ndjson(*chunks: dict[str, Any]) -> bytes:
    lines: list[str] = []
    for chunk in chunks:
        lines.append(json.dumps(chunk, ensure_ascii=False))

    return ("\n".join(lines) + "\n").encode()


class TestOllamaChatModel:
    """Wire-формат /api/chat: thinking, вызовы объектами, format по схеме."""

    async def test_stream_thinking_content_and_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            body = _ndjson(
                {"message": {"role": "assistant", "thinking": "думаю"}},
                {"message": {"role": "assistant", "content": "ответ"}},
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"function": {"name": "probe", "arguments": {"q": "x"}}}
                        ],
                    }
                },
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 5,
                    "eval_count": 4,
                },
            )
            return httpx.Response(200, content=body)

        reply = _reply_of(await _events(_ollama(monkeypatch, handler), REQUEST))

        if seen[0] != "https://fake/v1/api/chat":
            raise AssertionError(seen)
        if reply.content != "ответ" or reply.reasoning != "думаю":
            raise AssertionError(reply)
        if reply.tool_calls[0].arguments != {"q": "x"} or not reply.tool_calls[0].id:
            raise AssertionError(reply.tool_calls)
        if reply.usage.input_tokens != 5 or reply.usage.output_tokens != 4:
            raise AssertionError(reply.usage)

    async def test_reply_schema_goes_as_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            body = {
                "message": {"role": "assistant", "content": '{"text": "ok"}'},
                "done": True,
                "done_reason": "stop",
            }
            return httpx.Response(200, json=body)

        request = REQUEST.model_copy(update={"reply_schema": SCHEMA, "stream": False})
        reply = await _ollama(monkeypatch, handler).reply(request)

        if seen[0]["format"] != dict(SCHEMA.parameters):
            raise AssertionError(f"схема ушла полем format: {seen[0]}")
        if reply.content != '{"text": "ok"}':
            raise AssertionError(reply)

    async def test_length_done_reason_is_an_honest_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = _ndjson(
                {"message": {"role": "assistant", "content": "нач"}},
                {
                    "message": {"role": "assistant"},
                    "done": True,
                    "done_reason": "length",
                },
            )
            return httpx.Response(200, content=body)

        with pytest.raises(LlmError, match="token ceiling"):
            await _events(_ollama(monkeypatch, handler), REQUEST)
