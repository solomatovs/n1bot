"""Стенд модели отвечает по обоим протоколам и воспроизводит каждый отказ.

Через FakeLlmApp ходят настоящие клиенты boba-llm-http: openai-совместимый
и нативный ollama, в потоке и без. Проверяется, что отказ доходит до клиента
в ожидаемом виде: ошибкой LlmError либо ответом, который агенту предстоит
распознать самому (неизвестный инструмент, пустой id, повтор id, без учёта,
пустой ответ, теги <think>). Оборванный поток оба клиента сегодня считают
полным ответом — это зафиксировано здесь как факт, который правится на
этапе 2 плана агента.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

import pytest
import uvicorn

from boba.llm.chat import (
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    LlmError,
    ToolCall,
    ToolSpec,
)
from boba.llm.http.ollama import OllamaProvider
from boba.llm.http.openai import OpenAiProvider
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes
from boba.stand.ui.fake_llm import FailureName, FakeLlmApp, ScenarioName
from boba.stand.ui.stand import free_port
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import HttpConnection, NoneAuth, UrlScheme

pytestmark = pytest.mark.anyio

HANG_SEC = 2.0
CLIENT_TIMEOUT_SEC = 0.5


class Protocol(StrEnum):
    """Протокол клиента: маршрут стенда и корень соединения."""

    OPENAI = "openai"
    OLLAMA = "ollama"

    @property
    def path(self) -> str:
        if self is Protocol.OPENAI:
            return "/v1"

        return "/"


@dataclass(frozen=True)
class Outcome:
    """Что увидел клиент: ответ либо текст ошибки."""

    reply: ChatReply | None
    error: str
    deltas: int

    def failed_with(self, fragment: str) -> bool:
        return fragment in self.error


@dataclass(frozen=True)
class StatusCase:
    """Отказ статусом: маркер, код ответа и фрагмент текста ошибки."""

    failure: FailureName
    status: str
    fragment: str


class StandClients:
    """Оба клиента boba-llm-http на один запущенный стенд."""

    TOOLS = (
        ToolSpec(
            name="connection_list",
            description="lists connections",
            parameters={"type": "object", "properties": {}},
        ),
    )

    def __init__(self, port: int) -> None:
        self._port = port
        self._providers = LlmProviders(LlmProviderTypes.installed())

    def chat(self, protocol: Protocol) -> ChatModel:
        connection = HttpConnection(
            scheme=UrlScheme.HTTP,
            host="127.0.0.1",
            port=self._port,
            path=protocol.path,
            timeout_sec=CLIENT_TIMEOUT_SEC,
            retry_attempts=1,
            auth=NoneAuth(method="none"),
        )
        transport = HttpTransportConfig()
        if protocol is Protocol.OPENAI:
            provider = OpenAiProvider(
                kind="openai", connection=connection, transport=transport
            )
        else:
            provider = OllamaProvider(
                kind="ollama", connection=connection, transport=transport
            )

        return self._providers.chat(
            ChatModelConfig(provider=provider, model="fake-model")
        )

    async def ask(self, protocol: Protocol, text: str, stream: bool) -> Outcome:
        request = ChatRequest(
            messages=[ChatTurn(role=ChatRole.USER, content=text)],
            tools=self.TOOLS,
            stream=stream,
        )

        return await self.play(protocol, request)

    async def play(self, protocol: Protocol, request: ChatRequest) -> Outcome:
        deltas = 0
        reply: ChatReply | None = None
        try:
            async for event in self.chat(protocol).chat(request):
                if isinstance(event, ChatReply):
                    reply = event
                    continue

                deltas += 1
        except LlmError as exc:
            return Outcome(reply=None, error=str(exc), deltas=deltas)

        return Outcome(reply=reply, error="", deltas=deltas)

    async def aclose(self) -> None:
        await self._providers.aclose()


@pytest.fixture
async def stand_port() -> AsyncIterator[int]:
    port = free_port()
    app = FakeLlmApp(token_delay_sec=0.0, hang_sec=HANG_SEC)
    config = uvicorn.Config(
        app.asgi(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)

    try:
        yield port
    finally:
        server.should_exit = True
        await serving


@pytest.fixture
async def clients(stand_port: int) -> AsyncIterator[StandClients]:
    stand = StandClients(stand_port)
    try:
        yield stand
    finally:
        await stand.aclose()


def marker(failure: FailureName) -> str:
    return f"{ScenarioName.ANSWER.value} {failure.value}"


class TestBothProtocolsAnswer:
    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_answer_scenario(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, ScenarioName.ANSWER.value, stream)

        assert outcome.reply is not None, outcome.error
        assert outcome.reply.content.startswith("Here is a plain streamed answer")
        assert outcome.reply.usage.input_tokens == 11
        assert (outcome.deltas > 0) is stream

    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_tool_scenario_runs_two_turns(
        self, clients: StandClients, protocol: Protocol
    ) -> None:
        first = await clients.ask(protocol, ScenarioName.TOOL.value, True)

        assert first.reply is not None, first.error
        assert [call.name for call in first.reply.tool_calls] == ["connection_list"]

        call = first.reply.tool_calls[0]
        request = ChatRequest(
            messages=[
                ChatTurn(role=ChatRole.USER, content=ScenarioName.TOOL.value),
                ChatTurn(
                    role=ChatRole.ASSISTANT,
                    reasoning=first.reply.reasoning,
                    tool_calls=[
                        ToolCall(id=call.id, name=call.name, arguments=call.arguments)
                    ],
                ),
                ChatTurn(role=ChatRole.TOOL, content="[]", tool_call_id=call.id),
            ],
            tools=StandClients.TOOLS,
        )
        second = await clients.play(protocol, request)

        assert second.reply is not None, second.error
        assert second.reply.content == "The connections are above"
        assert not second.reply.tool_calls


class TestFailuresReachTheClient:
    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    @pytest.mark.parametrize(
        "case",
        [
            StatusCase(FailureName.STATUS_500, "500", "internal server error"),
            StatusCase(FailureName.STATUS_429, "429", "rate limit exceeded"),
            StatusCase(FailureName.OVERFLOW, "400", "maximum context length"),
        ],
        ids=["500", "429", "overflow"],
    )
    async def test_status_failures(
        self,
        clients: StandClients,
        protocol: Protocol,
        stream: bool,
        case: StatusCase,
    ) -> None:
        outcome = await clients.ask(protocol, marker(case.failure), stream)

        assert outcome.reply is None
        assert outcome.failed_with(f"got {case.status}")
        assert outcome.failed_with(case.fragment)

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_hang_ends_with_read_timeout(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.HANG), stream)

        assert outcome.reply is None
        assert outcome.failed_with("ReadTimeout")

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_length_is_an_error_with_the_ceiling_named(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.LENGTH), stream)

        assert outcome.reply is None
        assert outcome.failed_with("token ceiling")
        assert (outcome.deltas > 0) is stream

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    @pytest.mark.parametrize(
        "failure", [FailureName.LENGTH_ARGUMENTS, FailureName.BROKEN_ARGUMENTS]
    )
    async def test_broken_call_arguments_are_errors(
        self,
        clients: StandClients,
        protocol: Protocol,
        stream: bool,
        failure: FailureName,
    ) -> None:
        outcome = await clients.ask(protocol, marker(failure), stream)

        assert outcome.reply is None
        assert outcome.error

    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_cut_body_without_stream_is_an_error(
        self, clients: StandClients, protocol: Protocol
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.CUT), False)

        assert outcome.reply is None
        assert outcome.failed_with("response body is not")

    @pytest.mark.parametrize("protocol", list(Protocol))
    async def test_cut_stream_passes_as_a_complete_reply_today(
        self, clients: StandClients, protocol: Protocol
    ) -> None:
        """Факт, не желание: поток без финального чанка клиент принимает за
        полный ответ с половиной текста и нулевым учётом. Правка — этап 2."""
        intact = await clients.ask(protocol, ScenarioName.ANSWER.value, True)
        outcome = await clients.ask(protocol, marker(FailureName.CUT), True)

        assert intact.reply is not None, intact.error
        assert outcome.reply is not None, outcome.error
        assert intact.reply.content.startswith(outcome.reply.content)
        assert len(outcome.reply.content) < len(intact.reply.content)
        assert outcome.reply.usage.input_tokens == 0


class TestRepliesTheAgentMustJudge:
    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_unknown_tool_call_is_delivered(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.UNKNOWN_TOOL), stream)

        assert outcome.reply is not None, outcome.error
        assert [call.name for call in outcome.reply.tool_calls] == ["no_such_tool"]

    @pytest.mark.parametrize("stream", [True, False])
    async def test_empty_id_stays_empty_on_openai(
        self, clients: StandClients, stream: bool
    ) -> None:
        outcome = await clients.ask(
            Protocol.OPENAI, marker(FailureName.EMPTY_ID), stream
        )

        assert outcome.reply is not None, outcome.error
        assert [call.id for call in outcome.reply.tool_calls] == [""]

    @pytest.mark.parametrize("stream", [True, False])
    async def test_empty_id_is_minted_by_the_ollama_client(
        self, clients: StandClients, stream: bool
    ) -> None:
        outcome = await clients.ask(
            Protocol.OLLAMA, marker(FailureName.EMPTY_ID), stream
        )

        assert outcome.reply is not None, outcome.error
        assert len(outcome.reply.tool_calls) == 1
        assert len(outcome.reply.tool_calls[0].id) == 32

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_duplicate_ids_are_delivered_as_is(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.DUPLICATE_ID), stream)

        assert outcome.reply is not None, outcome.error
        assert [call.id for call in outcome.reply.tool_calls] == [
            "call_dup",
            "call_dup",
        ]

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_missing_usage_is_zero(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.NO_USAGE), stream)

        assert outcome.reply is not None, outcome.error
        assert outcome.reply.content
        assert outcome.reply.usage.input_tokens == 0
        assert outcome.reply.usage.output_tokens == 0

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_empty_reply_is_delivered(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.EMPTY), stream)

        assert outcome.reply is not None, outcome.error
        assert outcome.reply.content == ""
        assert outcome.reply.reasoning == ""
        assert not outcome.reply.tool_calls

    @pytest.mark.parametrize("protocol", list(Protocol))
    @pytest.mark.parametrize("stream", [True, False])
    async def test_think_tags_stay_inside_content(
        self, clients: StandClients, protocol: Protocol, stream: bool
    ) -> None:
        outcome = await clients.ask(protocol, marker(FailureName.THINK_TAGS), stream)

        assert outcome.reply is not None, outcome.error
        assert outcome.reply.content.startswith("<think>")
        assert outcome.reply.reasoning == ""
