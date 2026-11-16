"""Реальный HTTP-поток: JSON из SQL, немедленные дельты и завершение ответа."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
from agent_stand import AgentSystem, InferenceServer
from starlette.responses import JSONResponse, Response, StreamingResponse

from boba.agent import models as m
from boba.agent.history import HistoryStore
from boba.agent.llm import Generation, UserInference
from boba.agent.runtime import RecordFactory
from boba.llm.chat import LlmError, LlmServiceError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
def transport(system: AgentSystem) -> UserInference:
    return system.llm.for_user(system.owner, system.providers)


async def test_sql_body_is_unchanged_and_text_arrives_before_response_finishes(  # noqa: PLR0915
    transport: UserInference,
    history: HistoryStore,
    inference: InferenceServer,
    agent_timeout: float,
) -> None:
    release = asyncio.Event()
    emitted = asyncio.Event()
    deltas = []
    output = Generation()
    session_id = "http-stream-" + uuid4().hex
    request_id = uuid4().hex
    user = RecordFactory("test").make(
        m.UserMessageRecord,
        payload={"content": [m.TextPart(text="Hello 世界\r\n")]},
    )
    rows = [
        RecordFactory("test").make(
            m.ModelRecord,
            payload={"provider": "test", "value": {"model": "sql-model"}},
        ),
        RecordFactory("test").make(
            m.ProviderParametersRecord,
            payload={"value": {"vendor_option": {"enabled": True}}},
        ),
        user,
        RecordFactory("test").make(
            m.StreamingRecord,
            operation_id=request_id,
            payload={"value": {"stream": True}},
        ),
    ]

    async def body() -> AsyncGenerator[bytes, None]:
        yield b': keepalive\r\n\r\nevent: chunk\r\nid: 1\r\ndata: {"choices":\r\n'
        yield 'data: [{"delta":{"content":"Начало 世界\\r\\n"}}]}\r\n\r\n'.encode()
        await release.wait()
        yield b'data: {"choices":[{"delta":{"reasoning":"thought"}}]}\n\n'
        yield (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"one",'
            b'"function":{"name":"first","arguments":"{\\"x\\":"}},'
            b'{"index":1,"id":"two","function":{"name":"second",'
            b'"arguments":"{}"}}]}}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"one",'
            b'"function":{"arguments":"1}"}}]}}]}\n\n'
        )
        yield (
            b'data: {"choices":[{"delta":{"content":"end"},'
            b'"finish_reason":"tool_calls"}]}\n\n'
        )
        yield (
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
            b'"prompt_tokens_details":{"cached_tokens":2},'
            b'"completion_tokens_details":{"reasoning_tokens":1}}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    async def emit(position: int, kind: str, text: str) -> None:
        deltas.append((position, kind, text))
        emitted.set()

    inference.response = StreamingResponse(body(), media_type="text/event-stream")
    try:
        await history.append(session_id, rows)
        draft = await history.openai(session_id, request_id)
        task = asyncio.create_task(
            transport.generate("test", draft.request, output, emit)
        )
        try:
            async with asyncio.timeout(agent_timeout):
                await emitted.wait()

            assert not task.done()
            assert deltas == [(1, "text", "Начало 世界\r\n")]
            assert json.loads(inference.requests[0]) == draft.request
            assert "stream_options" not in json.loads(inference.requests[0])
            release.set()
            async with asyncio.timeout(agent_timeout):
                await task

        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        assert output.status == 200
        assert output.finish_reason == "tool_calls"
        assert output.usage == m.TokenUsage(
            input_tokens=7, output_tokens=3, cache_read_tokens=2, reasoning_tokens=1
        )
        assert output.content() == (
            m.TextPart(text="Начало 世界\r\n"),
            m.ReasoningPart(text="thought", signature=None, format=None),
            m.ToolCallReferencePart(call_id="one"),
            m.ToolCallReferencePart(call_id="two"),
            m.TextPart(text="end"),
        )
        assert deltas[-1] == (5, "text", "end")
        assert (await history.openai(session_id, request_id)).request == draft.request

    finally:
        async with history.pool.connection() as conn:
            await conn.execute(
                "delete from boba.records where session_id = %s", (session_id,)
            )


async def test_non_stream_response_keeps_both_calls_and_refusal(
    transport: UserInference,
    inference: InferenceServer,
) -> None:
    inference.response = JSONResponse(
        {
            "choices": [
                {
                    "message": {
                        "refusal": "Refused",
                        "tool_calls": [
                            {
                                "id": "one",
                                "function": {"name": "first", "arguments": "{}"},
                            },
                            {
                                "id": "two",
                                "function": {"name": "second", "arguments": '{"x":2}'},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    output = Generation()
    deltas = []

    async def emit(position: int, kind: str, text: str) -> None:
        deltas.append((position, kind, text))

    await transport.generate("test", {"model": "test", "messages": []}, output, emit)
    assert output.content() == (
        m.TextPart(text="Refused"),
        m.ToolCallReferencePart(call_id="one"),
        m.ToolCallReferencePart(call_id="two"),
    )
    assert output.usage is None
    assert deltas == [(1, "text", "Refused")]


@pytest.mark.parametrize(
    "tail",
    [
        b"data: [DONE]\n\n",
        b"data: invalid-json\n\n",
        b'data: {"error":{"message":"provider failed"}}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"content_filter"}]}\n\n',
    ],
)
async def test_broken_response_preserves_only_received_text(
    transport: UserInference, inference: InferenceServer, tail: bytes
) -> None:
    body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n' + tail
    inference.response = Response(body, media_type="text/event-stream")
    output = Generation()
    deltas = []

    async def emit(position: int, kind: str, text: str) -> None:
        deltas.append((position, kind, text))

    with pytest.raises(LlmError):
        await transport.generate(
            "test", {"model": "test", "stream": True}, output, emit
        )

    assert output.content(partial=True) == (m.TextPart(text="partial"),)
    assert deltas == [(1, "text", "partial")]
    assert len(inference.requests) == 1


async def test_status_is_preserved_and_retry_belongs_to_agent(
    transport: UserInference,
    inference: InferenceServer,
) -> None:
    inference.response = Response("unavailable", status_code=503)
    output = Generation()

    async def emit(position: int, kind: str, text: str) -> None:
        pytest.fail("An HTTP error must not emit text")

    with pytest.raises(LlmServiceError) as caught:
        await transport.generate(
            "test", {"model": "test", "stream": True}, output, emit
        )

    assert caught.value.status == 503
    assert transport.retry_delay(output, caught.value, 1) == 0
    assert transport.retry_delay(output, caught.value, 3) is None
    assert len(inference.requests) == 1


async def test_cancellation_closes_http_while_callback_is_waiting(
    transport: UserInference,
    inference: InferenceServer,
    agent_timeout: float,
) -> None:
    emitted = asyncio.Event()
    disconnected = asyncio.Event()
    blocked = asyncio.Event()
    output = Generation()

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await blocked.wait()

        finally:
            disconnected.set()

    async def emit(position: int, kind: str, text: str) -> None:
        emitted.set()
        await blocked.wait()

    inference.response = StreamingResponse(body(), media_type="text/event-stream")
    task = asyncio.create_task(
        transport.generate("test", {"model": "test", "stream": True}, output, emit)
    )
    try:
        async with asyncio.timeout(agent_timeout):
            await emitted.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            await disconnected.wait()

        assert output.content(partial=True) == (m.TextPart(text="partial"),)
        assert not output.finish_reason
        assert len(inference.requests) == 1

    finally:
        blocked.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_output_limit_preserves_text_without_incomplete_tool_call(
    transport: UserInference,
    inference: InferenceServer,
) -> None:
    inference.response = Response(
        b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"one",'
        b'"function":{"name":"first","arguments":"{"}}]},'
        b'"finish_reason":"length"}]}\n\n'
        b"data: [DONE]\n\n",
        media_type="text/event-stream",
    )
    output = Generation()
    deltas = []

    async def emit(position: int, kind: str, text: str) -> None:
        deltas.append((position, kind, text))

    await transport.generate("test", {"model": "test", "stream": True}, output, emit)
    assert output.finish_reason == "length"
    assert output.content(partial=True) == (m.TextPart(text="partial"),)
    assert deltas == [(1, "text", "partial")]
