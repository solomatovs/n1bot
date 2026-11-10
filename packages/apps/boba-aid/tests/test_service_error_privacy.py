"""Приватность отказов модели и агента на HTTP, в чате и в истории."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from http import HTTPStatus
from typing import ClassVar

import pytest

from boba.agent.asking import Errored, ModelAsk, Overflowed
from boba.agent.events import AgentEvent, EventSink
from boba.agent.records import (
    ApiErrorRecord,
    CompactionPhase,
    CompactionRecord,
    CompactTrigger,
    NoticeLevel,
    TurnEndRecord,
    TurnOutcome,
    Usage,
)
from boba.aid.agent.client import (
    AgentFailureKind,
    AgentProcessError,
    AgentRequestError,
    LinkFailures,
)
from boba.aid.infra.notices import FailureText, UserFailures
from boba.aid.rendering.failures import FailureReports
from boba.cancellation import RunCancellation
from boba.cli.acp_server import AcpMapping
from boba.llm.chat import (
    ChatDelta,
    ChatEvent,
    ChatModel,
    ChatRequest,
    LlmError,
    LlmServiceError,
    LlmTrouble,
)
from boba.llm.http.ollama import OllamaBackend, OllamaProvider
from boba.llm.http.openai import OpenAiBackend, OpenAiProvider
from boba.llm.providers import ChatModelConfig, EmbeddingModelConfig
from boba.toolkit.failure import FailurePacker
from boba.transport.acp.protocol import SessionFailureData, SessionFailureKind
from boba.transport.http.connection import HttpConnection, UrlScheme

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Этим границам не нужна HTTP-сессия Chainlit."""


@dataclass
class RefusalServer:
    """Локальный HTTP-сервер возвращает произвольный отказ настоящему клиенту."""

    PRIVATE: ClassVar[str] = "http://private-model.local:18743/private-api?key=secret"
    status: HTTPStatus = HTTPStatus.BAD_GATEWAY
    body: bytes = b""
    port: int = 0
    requests: int = 0

    def connection(self) -> HttpConnection:
        return HttpConnection(
            scheme=UrlScheme.HTTP,
            host="127.0.0.1",
            port=self.port,
            path="/private-model-route",
            retry_attempts=1,
        )

    async def answer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            head = (
                f"HTTP/1.1 {self.status.value} {self.status.phrase}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(self.body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(head.encode() + self.body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.fixture
async def refusal() -> AsyncIterator[RefusalServer]:
    endpoint = RefusalServer()
    endpoint.body = json.dumps({"error": {"message": endpoint.PRIVATE}}).encode()
    server = await asyncio.start_server(endpoint.answer, "127.0.0.1", 0)
    endpoint.port = server.sockets[0].getsockname()[1]
    async with server:
        yield endpoint


class SilentEvents(EventSink):
    """Приёмник без UI для проверки итогов ModelAsk."""

    async def emit(self, event: AgentEvent) -> None:
        return


class BrokenModel(ChatModel):
    """Провайдер с неизвестной ошибкой после первого события ответа."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        yield ChatDelta(content="partial")
        raise self.error


class TestModelPrivacy:
    async def test_unknown_provider_failure_is_owned_after_the_first_chunk(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cause = LookupError(RefusalServer.PRIVATE)
        result = await ModelAsk(BrokenModel(cause), SilentEvents()).attempt(
            ChatRequest(messages=[]), RunCancellation(), True, "1"
        )
        assert isinstance(result, Errored)
        assert "private" not in json.dumps(asdict(result))
        assert RefusalServer.PRIVATE in caplog.text

    async def test_cancellation_keeps_its_control_flow(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await ModelAsk(
                BrokenModel(asyncio.CancelledError()), SilentEvents()
            ).attempt(ChatRequest(messages=[]), RunCancellation(), True, "1")

    async def test_unknown_http_payload_failure_is_owned(
        self,
        refusal: RefusalServer,
    ) -> None:
        provider = OpenAiProvider(kind="openai", connection=refusal.connection())
        backend = OpenAiBackend(provider)
        model = backend.chat(ChatModelConfig(provider=provider, model="private-model"))
        request = ChatRequest(messages=[], sampling={"bad": object()})
        try:
            with pytest.raises(LlmError) as caught:
                async for _ in model.chat(request):
                    pass
        finally:
            await backend.aclose()

        assert caught.value.__cause__ is not None
        assert refusal.requests == 0
        self._public(caught.value, refusal)

    @pytest.mark.parametrize("stream", [True, False])
    @pytest.mark.parametrize("kind", ["openai", "ollama"])
    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    async def test_http_failure_is_safe_in_every_public_view(
        self,
        refusal: RefusalServer,
        stream: bool,
        kind: str,
        status: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        refusal.status = HTTPStatus(status)
        if kind == "openai":
            provider = OpenAiProvider(kind=kind, connection=refusal.connection())
            backend = OpenAiBackend(provider)
        else:
            provider = OllamaProvider(kind="ollama", connection=refusal.connection())
            backend = OllamaBackend(provider)

        model = backend.chat(ChatModelConfig(provider=provider, model="private-model"))
        try:
            with pytest.raises(LlmServiceError) as caught:
                async for _ in model.chat(ChatRequest(messages=[], stream=stream)):
                    pass
        finally:
            await backend.aclose()

        self._public(caught.value, refusal)
        assert caught.value.detail.startswith(f"HTTP {status}")
        assert refusal.PRIVATE in caplog.text
        assert refusal.requests == 1

    async def test_embedding_failure_does_not_reveal_anonymous_endpoint(
        self,
        refusal: RefusalServer,
    ) -> None:
        provider = OpenAiProvider(kind="openai", connection=refusal.connection())
        backend = OpenAiBackend(provider)
        embedder = backend.embedding(
            EmbeddingModelConfig(
                provider=provider,
                model="private-model",
                dim=3,
                batch_size=1,
                progress_every=1,
            )
        )
        try:
            with pytest.raises(LlmError) as caught:
                await embedder.embed_query("hello")
        finally:
            await backend.aclose()

        self._public(caught.value, refusal)

    async def test_attempt_and_overflow_keep_only_public_fields(
        self,
        refusal: RefusalServer,
    ) -> None:
        provider = OpenAiProvider(kind="openai", connection=refusal.connection())
        backend = OpenAiBackend(provider)
        model = backend.chat(ChatModelConfig(provider=provider, model="private-model"))
        ask = ModelAsk(model, SilentEvents())
        try:
            result = await ask.attempt(
                ChatRequest(messages=[]), RunCancellation(), False, "1"
            )
            assert isinstance(result, Errored)
            public = json.dumps(asdict(result))
            assert "private" not in public
            assert "127.0.0.1" not in public

            refusal.status = HTTPStatus.BAD_REQUEST
            refusal.body = (
                "maximum context length is 100 tokens, requested 130 tokens: "
                + refusal.PRIVATE
            ).encode()
            overflow = await ask.attempt(
                ChatRequest(messages=[]), RunCancellation(), False, "2"
            )
            assert isinstance(overflow, Overflowed)
            assert overflow.excess == 30
            assert "private" not in json.dumps(asdict(overflow))
        finally:
            await backend.aclose()

    async def test_malformed_response_and_wrapped_cause_are_safe(
        self,
        refusal: RefusalServer,
    ) -> None:
        refusal.status = HTTPStatus.OK
        refusal.body = refusal.PRIVATE.encode()
        provider = OpenAiProvider(kind="openai", connection=refusal.connection())
        backend = OpenAiBackend(provider)
        model = backend.chat(ChatModelConfig(provider=provider, model="private-model"))
        try:
            with pytest.raises(LlmError) as caught:
                async for _ in model.chat(ChatRequest(messages=[], stream=False)):
                    pass
        finally:
            await backend.aclose()

        self._public(caught.value, refusal)
        try:
            raise RuntimeError("The embedding tool failed") from caught.value
        except RuntimeError as wrapped:
            serialized = FailurePacker().pack(wrapped).model_dump_json()

        assert "private" not in serialized
        assert "127.0.0.1" not in serialized

    def _public(self, error: LlmError, refusal: RefusalServer) -> None:
        report = FailureReports().describe(error)
        public = str(error) + str(report.view) + str(report.history)
        public += FailurePacker().pack(error).model_dump_json()
        for secret in ("private", "127.0.0.1", str(refusal.port), "18743", "secret"):
            assert secret not in public

        assert "private" in error.diagnostic
        assert report.log == error.diagnostic


class TestAgentPrivacy:
    def test_resume_refusal_hides_peer_and_remote_details(self) -> None:
        data = SessionFailureData(kind=SessionFailureKind.TOOLS_UNAVAILABLE)
        wire = data.model_dump(mode="json")
        wire["details"] = RefusalServer.PRIVATE
        error = AgentRequestError(
            "session/resume", -32603, "Internal error", wire, RefusalServer.PRIVATE
        )
        notice = UserFailures().of(error)
        report = FailureReports().describe(error)
        assert notice.text is FailureText.TOOLS_UNAVAILABLE
        assert notice.detail == ""
        assert "private" not in str(error) + str(report.view) + str(report.history)
        assert RefusalServer.PRIVATE in report.log
        assert error.failure.kind is AgentFailureKind.TOOLS_UNAVAILABLE

    def test_unknown_client_failure_is_owned_and_keeps_its_cause(self) -> None:
        cause = LookupError(RefusalServer.PRIVATE)
        with (
            pytest.raises(AgentProcessError) as caught,
            LinkFailures().boundary(RefusalServer.PRIVATE, "resume"),
        ):
            raise cause

        assert caught.value.__cause__ is cause
        assert RefusalServer.PRIVATE in caught.value.diagnostic
        assert caught.value.failure.kind is AgentFailureKind.INTERNAL_ERROR
        assert "private" not in str(caught.value)
        assert UserFailures().of(caught.value).detail == ""

    def test_cancellation_is_not_an_agent_failure(self) -> None:
        with (
            pytest.raises(asyncio.CancelledError),
            LinkFailures().boundary(RefusalServer.PRIVATE, "resume"),
        ):
            raise asyncio.CancelledError()


class TestHistoryPrivacy:
    def test_legacy_retry_keeps_status_without_the_response_body(self) -> None:
        record = ApiErrorRecord(
            uuid="record",
            parent_uuid=None,
            timestamp="2026-01-01T00:00:00Z",
            session_id="session",
            version="0",
            error=RefusalServer.PRIVATE,
            retry_in_ms=1000,
            retry_attempt=1,
            max_retries=2,
            trouble=LlmTrouble.DOWN,
            detail=f"HTTP 503 Service Unavailable: {RefusalServer.PRIVATE}",
        )
        restored = ApiErrorRecord.model_validate_json(record.model_dump_json())
        notice = AcpMapping().retry(restored)
        assert notice.description == "HTTP 503 Service Unavailable"
        assert "private" not in notice.model_dump_json()

    def test_legacy_turn_end_hides_the_provider_failure(self) -> None:
        record = TurnEndRecord(
            uuid="record",
            parent_uuid=None,
            timestamp="2026-01-01T00:00:00Z",
            session_id="session",
            version="0",
            error=RefusalServer.PRIVATE,
            outcome=TurnOutcome.API_ERROR,
            notice=RefusalServer.PRIVATE,
            level=NoticeLevel.ERROR,
            detail=RefusalServer.PRIVATE,
            usage=Usage(),
            num_turns=1,
            duration_ms=1,
        )
        restored = TurnEndRecord.model_validate_json(record.model_dump_json())
        for update in AcpMapping().turn_ended(restored):
            assert "private" not in update.model_dump_json()

    @pytest.mark.parametrize("prefix", ["the summary request failed: ", ""])
    def test_legacy_compaction_hides_the_provider_failure(self, prefix: str) -> None:
        record = CompactionRecord(
            uuid="record",
            parent_uuid=None,
            timestamp="2026-01-01T00:00:00Z",
            session_id="session",
            version="0",
            error=prefix + RefusalServer.PRIVATE,
            compaction_id="compaction",
            phase=CompactionPhase.FAILED,
            trigger=CompactTrigger.AUTO,
            command_uuid=None,
        )
        restored = CompactionRecord.model_validate_json(record.model_dump_json())
        shown = AcpMapping().compaction(restored)
        assert "private" not in shown.model_dump_json()
        assert shown.error
