# кадры ниже приведены так, как их отдаёт api_server: одной строкой
# ruff: noqa: E501
import json

import httpx
import pytest

from boba.hermes.agent.api import HermesApiClient
from boba.hermes.agent.events import (
    HermesAssistantCompleted,
    HermesAssistantDelta,
    HermesErrorEvent,
    HermesEvent,
    HermesEventCodec,
    HermesEventName,
    HermesEventStream,
    HermesRunCompleted,
    HermesToolEvent,
    HermesToolProgress,
)
from boba.hermes.errors import ExternalServiceError, InternalServiceError
from boba.hermes.infra.config import HermesConfig

pytestmark = pytest.mark.anyio

PROFILE = "solomatovs"
SESSION = "6f1c0e3c-0000-4000-8000-000000000001"

# кадры сняты с живого api_server: POST /api/sessions/<id>/chat/stream
TURN = """event: run.started
data: {"user_message": {"role": "user", "content": "покажи вывод echo"}, "session_id": "S", "run_id": "run_1", "seq": 1, "ts": 1785232466.35}

event: message.started
data: {"message": {"id": "msg_1", "role": "assistant"}, "session_id": "S", "run_id": "run_1", "seq": 2, "ts": 1785232466.36}

event: tool.started
data: {"message_id": "msg_1", "tool_name": "terminal", "preview": "echo boba-hermes", "args": {"command": "echo boba-hermes"}, "session_id": "S", "run_id": "run_1", "seq": 3, "ts": 1785232488.07}

: keepalive

event: tool.completed
data: {"message_id": "msg_1", "tool_name": "terminal", "preview": null, "args": null, "session_id": "S", "run_id": "run_1", "seq": 4, "ts": 1785232491.64}

event: assistant.delta
data: {"message_id": "msg_1", "delta": "boba", "session_id": "S", "run_id": "run_1", "seq": 5, "ts": 1785232494.35}

event: assistant.delta
data: {"message_id": "msg_1", "delta": "-hermes", "session_id": "S", "run_id": "run_1", "seq": 6, "ts": 1785232494.36}

event: tool.progress
data: {"message_id": "msg_1", "tool_name": "_thinking", "delta": "boba-hermes", "session_id": "S", "run_id": "run_1", "seq": 7, "ts": 1785232494.37}

event: assistant.completed
data: {"session_id": "S", "message_id": "msg_1", "content": "boba-hermes", "completed": true, "partial": false, "interrupted": false, "run_id": "run_1", "seq": 8, "ts": 1785232494.38}

event: run.completed
data: {"session_id": "S", "message_id": "msg_1", "completed": true, "messages": [{"role": "assistant", "content": "boba-hermes", "finish_reason": "stop"}], "usage": {"input_tokens": 16157, "output_tokens": 125, "total_tokens": 16282}, "run_id": "run_1", "seq": 9, "ts": 1785232494.38}

event: done
data: {"session_id": "S", "run_id": "run_1", "seq": 10, "ts": 1785232494.39}

"""


async def lines_of(text: str):
    for line in text.split("\n"):
        yield line


async def events_of(text: str) -> list[HermesEvent]:
    return [event async for event in HermesEventStream().of(lines_of(text))]


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Разбор событий идёт без chainlit и postgres.

    Подменяет autouse-фикстуру из conftest, которая поднимает их для тестов
    data layer.
    """


@pytest.fixture
def config() -> HermesConfig:
    return HermesConfig(base_url="http://hermes:8642/", api_key="k" * 32)


def client_for(config: HermesConfig, handler) -> HermesApiClient:
    transport = httpx.MockTransport(handler)
    return HermesApiClient(
        httpx.AsyncClient(transport=transport), config, profile=PROFILE
    )


async def test_stream_keeps_event_order():
    events = await events_of(TURN)

    assert [event.name for event in events] == [
        HermesEventName.RUN_STARTED,
        HermesEventName.MESSAGE_STARTED,
        HermesEventName.TOOL_STARTED,
        HermesEventName.TOOL_COMPLETED,
        HermesEventName.ASSISTANT_DELTA,
        HermesEventName.ASSISTANT_DELTA,
        HermesEventName.TOOL_PROGRESS,
        HermesEventName.ASSISTANT_COMPLETED,
        HermesEventName.RUN_COMPLETED,
        HermesEventName.DONE,
    ]


async def test_stream_parses_answer_and_tool():
    events = await events_of(TURN)
    by_name = {event.name: event for event in events}

    started = by_name[HermesEventName.TOOL_STARTED]
    assert isinstance(started, HermesToolEvent)
    assert started.tool_name == "terminal"
    # превью — то, что показывается как ввод шага
    assert started.preview == "echo boba-hermes"
    assert started.args == {"command": "echo boba-hermes"}

    completed = by_name[HermesEventName.TOOL_COMPLETED]
    assert isinstance(completed, HermesToolEvent)
    # результат вызова в событии не приходит, он остаётся в истории сессии
    assert completed.preview is None

    deltas = [e for e in events if isinstance(e, HermesAssistantDelta)]
    assert "".join(delta.delta for delta in deltas) == "boba-hermes"

    answer = by_name[HermesEventName.ASSISTANT_COMPLETED]
    assert isinstance(answer, HermesAssistantCompleted)
    assert answer.content == "boba-hermes"
    assert answer.interrupted is False


async def test_stream_parses_usage():
    events = await events_of(TURN)
    done = next(e for e in events if isinstance(e, HermesRunCompleted))

    assert done.usage.total_tokens == 16282
    assert done.messages[0]["finish_reason"] == "stop"


async def test_reasoning_comes_as_thinking_tool():
    events = await events_of(TURN)
    progress = next(e for e in events if isinstance(e, HermesToolProgress))

    assert progress.tool_name == "_thinking"
    assert progress.delta == "boba-hermes"


async def test_stream_ignores_comments_and_empty_frames():
    text = ": keepalive\n\n\n: stream closed\n\n"

    assert await events_of(text) == []


async def test_stream_skips_unreadable_frame():
    text = 'event: assistant.delta\ndata: {"delta"\n\nevent: done\ndata: {}\n\n'

    assert [event.name for event in await events_of(text)] == [HermesEventName.DONE]


async def test_unknown_event_keeps_common_fields():
    codec = HermesEventCodec()

    event = codec.of("subagent.start", '{"run_id": "run_1", "seq": 3}')

    assert event is not None
    assert event.name == "subagent.start"
    assert event.run_id == "run_1"
    assert event.seq == 3


async def test_chat_stream_posts_message(config: HermesConfig):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=TURN.encode())

    events = [
        event
        async for event in client_for(config, handler).chat_stream(SESSION, "покажи")
    ]

    assert seen[0].url.path == f"/p/{PROFILE}/api/sessions/{SESSION}/chat/stream"
    assert json.loads(seen[0].read()) == {"message": "покажи"}
    assert [event.name for event in events][-1] == HermesEventName.DONE


async def test_chat_stream_reports_error_event(config: HermesConfig):
    body = 'event: error\ndata: {"message": "provider auth failed"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    events = [
        event
        async for event in client_for(config, handler).chat_stream(SESSION, "привет")
    ]

    assert isinstance(events[0], HermesErrorEvent)
    assert events[0].message == "provider auth failed"


async def test_chat_stream_on_missing_session(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "Session not found"}})

    with pytest.raises(InternalServiceError):
        async for _ in client_for(config, handler).chat_stream(SESSION, "привет"):
            pass


async def test_chat_stream_transport_failure_becomes_external(config: HermesConfig):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ExternalServiceError):
        async for _ in client_for(config, handler).chat_stream(SESSION, "привет"):
            pass
