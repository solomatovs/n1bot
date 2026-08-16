"""Журнал состояний обмена с LLM на реальном прогоне провайдера и агента.

Условия совпадают с боевыми: фабрика LogRecord подставляет поле `user`,
сессии chainlit нет, колбэки идут через настоящий callback-менеджер langchain,
который гасит сбои обработчика в WARNING — такие записи тест считает провалом.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import SecretStr
from ui.fake_llm import FakeLlmApp, ScenarioName
from uvicorn.logging import DefaultFormatter

from boba.chainlit.agent.chat_model import ReasoningChatOpenAI
from boba.chainlit.chat.tracing import LlmStateLog
from boba.chainlit.domain.session import LogUserMark
from boba.chainlit.infra.config import LOGGING_CONFIG
from boba.chainlit.infra.log_context import UserLogContext

pytestmark = pytest.mark.anyio

THREAD = "5c6e150c-5543-4fcc-9be9-bbc4c2523c38"
USER = "solomatovs"
MARK = f"{USER} {THREAD[:8]}"
TRACE_LOGGER = "boba.chainlit.chat.tracing"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: ход журнала не ходит в контекст chainlit."""


@pytest.fixture(autouse=True)
def log_factory(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Боевая фабрика LogRecord: поле `user` уже стоит в каждой записи."""
    UserLogContext.install()
    with caplog.at_level(logging.INFO):
        yield


@pytest.fixture
async def provider() -> AsyncIterator[httpx.AsyncClient]:
    """Фейковый OpenAI-совместимый провайдер прямо в процессе теста."""
    app = FakeLlmApp(token_delay_sec=0.0).asgi()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fake-llm",
    )
    try:
        yield client
    finally:
        await client.aclose()


class TestLlmStateLog:
    @staticmethod
    def _chat(provider: httpx.AsyncClient) -> ReasoningChatOpenAI:
        return ReasoningChatOpenAI(
            http_async_client=provider,
            model="fake-model",
            base_url="http://fake-llm/v1",
            api_key=SecretStr("fake-key"),
        )

    @staticmethod
    def _log() -> LlmStateLog:
        return LlmStateLog(LogUserMark(USER, THREAD))

    @staticmethod
    def _journal_tools() -> list[BaseTool]:
        def usage() -> str:
            return "journal usage"

        def cleanup(thread_id: str) -> str:
            raise ValueError(f"unknown thread: {thread_id}")

        return [
            StructuredTool.from_function(
                func=usage,
                name="stream_logs_usage",
                description="Journal usage",
            ),
            StructuredTool.from_function(
                func=cleanup,
                name="stream_logs_cleanup",
                description="Purge the journal of a thread",
            ),
        ]

    async def _stream_chat(
        self, provider: httpx.AsyncClient, scenario: ScenarioName
    ) -> None:
        chat = self._chat(provider)
        stream = chat.astream(scenario.value, config={"callbacks": [self._log()]})
        async for _chunk in stream:
            pass

    async def _stream_agent(
        self, provider: httpx.AsyncClient, scenario: ScenarioName
    ) -> None:
        """Ход как в проде: langgraph поверх модели, стрим сообщениями."""
        agent = create_agent(
            model=self._chat(provider),
            tools=self._journal_tools(),
            system_prompt="test agent",
        )
        stream = agent.astream(
            {"messages": [HumanMessage(content=scenario.value)]},
            stream_mode="messages",
            config={"callbacks": [self._log()]},
        )
        async for _chunk in stream:
            pass

    @staticmethod
    def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        records: list[logging.LogRecord] = []
        for record in caplog.records:
            if record.name != TRACE_LOGGER:
                continue

            records.append(record)

        return records

    @classmethod
    def _heads(cls, caplog: pytest.LogCaptureFixture) -> list[str]:
        heads: list[str] = []
        for record in cls._records(caplog):
            heads.append(record.getMessage().split(":")[0])

        return heads

    @classmethod
    def _lines(cls, caplog: pytest.LogCaptureFixture) -> list[str]:
        lines: list[str] = []
        for record in cls._records(caplog):
            lines.append(record.getMessage())

        return lines

    @staticmethod
    def _complaints(caplog: pytest.LogCaptureFixture) -> list[str]:
        """Жалобы любого логгера: сбой обработчика langchain прячет в WARNING."""
        complaints: list[str] = []
        for record in caplog.records:
            if record.levelno < logging.WARNING:
                continue

            complaints.append(f"{record.name}: {record.getMessage()}")

        return complaints

    async def test_streamed_turn_logs_every_stage(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_chat(provider, ScenarioName.THINKING_ANSWER)

        assert self._complaints(caplog) == []
        assert self._heads(caplog) == [
            "llm request started",
            "llm first token",
            "llm thinking started",
            "llm thinking finished",
            "llm answer started",
            "llm answer finished",
            "llm request finished",
        ]

    async def test_stage_lines_carry_size_and_duration(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_chat(provider, ScenarioName.THINKING_ANSWER)

        finished = next(
            line for line in self._lines(caplog) if line.startswith("llm thinking fin")
        )
        assert "33 chars in" in finished
        assert finished.endswith("ms")

    async def test_lines_are_formatted_with_user_and_thread(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Тот же форматтер, что и у приложения: метка обязана попасть в строку."""
        await self._stream_chat(provider, ScenarioName.ANSWER)

        spec = LOGGING_CONFIG["formatters"]["default"]
        formatter = DefaultFormatter(fmt=spec["fmt"], use_colors=False)
        formatted: list[str] = []
        for record in self._records(caplog):
            formatted.append(formatter.format(record))

        assert self._complaints(caplog) == []
        assert formatted
        for line in formatted:
            assert f"[{MARK}]" in line

    async def test_mark_does_not_leak_after_the_line(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Метка живёт только на время записи: чужие строки её не наследуют."""
        await self._stream_chat(provider, ScenarioName.ANSWER)

        assert LogUserMark.current() == ""

        record = logging.getLogger("probe").makeRecord(
            "probe", logging.INFO, "f.py", 1, "after the turn", (), None
        )
        assert getattr(record, UserLogContext.ATTRIBUTE) == UserLogContext.UNKNOWN

    async def test_answer_without_stream_is_logged_as_complete(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        chat = self._chat(provider)
        await chat.ainvoke(
            ScenarioName.THINKING_ANSWER.value, config={"callbacks": [self._log()]}
        )

        assert self._complaints(caplog) == []
        assert self._heads(caplog) == [
            "llm request started",
            "llm thinking complete",
            "llm answer complete",
            "llm request finished",
        ]
        assert "tokens in=11 out=7" in self._lines(caplog)[-1]

    async def test_tool_call_turn_logs_both_runs_and_the_call(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_agent(provider, ScenarioName.TOOL)

        heads = self._heads(caplog)
        assert self._complaints(caplog) == []
        assert heads.count("llm request started") == 2
        assert "tool stream_logs_usage started" in heads
        assert "tool stream_logs_usage finished" in heads
        assert heads.index("tool stream_logs_usage started") > heads.index(
            "llm request finished"
        )

    async def test_tool_line_reports_call_id_and_duration(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_agent(provider, ScenarioName.TOOL)

        finished = next(
            line
            for line in self._lines(caplog)
            if line.startswith("tool stream_logs_usage finished")
        )
        assert "call=call_stream_logs" in finished
        assert "output=13 chars" in finished

    async def test_failed_tool_is_logged_as_failed(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Инструмент падает по-настоящему: ошибка уходит наверх, ход её покажет."""
        with pytest.raises(ValueError, match="unknown thread"):
            await self._stream_agent(provider, ScenarioName.TOOL_ERROR)

        assert self._complaints(caplog) == []
        assert "tool stream_logs_cleanup failed" in self._heads(caplog)
