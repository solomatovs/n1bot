"""Журнал состояний обмена с LLM на реальном прогоне провайдера и агента.

Условия совпадают с боевыми: фабрика LogRecord подставляет поле `user`,
сессии chainlit нет, колбэки идут через настоящий callback-менеджер langchain,
который гасит сбои обработчика в WARNING — такие записи тест считает провалом.
Строки о вызовах инструментов пишет путь вызова узла чата (ChatCalls).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated

import pytest
from chainlit_stand import fake_openai_chat, in_process_llm
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from uvicorn.logging import DefaultFormatter

from boba.chainlit.agent.bridge import ChatModelBridge
from boba.chainlit.agent.flow import GraphSpec, PlainGraphBuilder
from boba.chainlit.chat.tracing import LlmStateLog
from boba.chainlit.infra.config import LOGGING_CONFIG
from boba.chainlit.infra.log_context import UserLogContext
from boba.chainlit.infra.providers import build_history_view
from boba.chainlit.rendering.mount import ChatCalls, ChatMount
from boba.connection_broker.sealing import SentConnections
from boba.identity.run import Runs
from boba.identity.session import LogUserMark
from boba.stand.refs import StandRefs
from boba.stand.ui.fake_llm import FakeLlmApp, ScenarioBook, ScenarioName
from boba.stand_core.context import CallStand, FakeTurn
from boba.toolkit.chain import CallAmbient
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import MarkdownResult
from boba.toolrun.hosted import ToolHosting
from boba.toolrun.stream_calls import LocalDagService

pytestmark = pytest.mark.anyio

THREAD = "5c6e150c-5543-4fcc-9be9-bbc4c2523c38"
USER = "solomatovs"
MARK = f"{USER} {THREAD[:8]}"
TRACE_LOGGERS = ("boba.chainlit.chat.tracing", "boba.chainlit.rendering.mount")
"""Кто пишет журнал хода: прогоны модели и вызовы инструментов."""


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
def provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Фейковый OpenAI-совместимый провайдер прямо в процессе теста."""
    in_process_llm(monkeypatch, FakeLlmApp(token_delay_sec=0.0).asgi())


class TestLlmStateLog:
    @staticmethod
    def _chat(provider: None) -> ChatModelBridge:
        return fake_openai_chat()

    @staticmethod
    def _log() -> LlmStateLog:
        return LlmStateLog(LogUserMark(USER, THREAD))

    @staticmethod
    def _stand_tools() -> list[PayloadTool]:
        @tool
        def connection_list() -> MarkdownResult:
            """Connections of the caller"""
            return MarkdownResult(text="connections")

        @tool
        def send_file(
            path: Annotated[str, Field(description="Workspace file")],
        ) -> MarkdownResult:
            """Send a workspace file"""
            raise ValueError(f"file not found: {path}")

        return [connection_list, send_file]

    async def _stream_chat(self, provider: None, scenario: ScenarioName) -> None:
        chat = self._chat(provider)
        stream = chat.astream(scenario.value, config={"callbacks": [self._log()]})
        async for _chunk in stream:
            pass

    async def _stream_agent(self, provider: None, scenario: ScenarioName) -> None:
        """Ход как в проде: граф хода поверх модели и порта инструментов,
        вызовы идут к телу путём ChatCalls под идущим ходом."""
        calls = CallStand()
        contexts = calls.contexts
        runs = Runs(contexts)
        service = LocalDagService(
            ToolHosting().toolset(self._stand_tools()),
            StandRefs.STREAM_CONFIG,
            (),
            StreamSpecs({}),
            CallAmbient(),
            ChatCalls(contexts, runs, SentConnections(), ChatMount(contexts, runs)),
        )
        spec = GraphSpec(
            chat=self._chat(provider),
            service=service,
            system_prompt="test agent",
            checkpointer=InMemorySaver(),
            history=build_history_view(service.names(), 30),
        )
        agent = PlainGraphBuilder().build(spec)

        with runs.open(calls.context(THREAD, login=USER), FakeTurn()):
            stream = agent.astream(
                {"messages": [HumanMessage(content=scenario.value)]},
                stream_mode="messages",
                config={
                    "callbacks": [self._log()],
                    "configurable": {"thread_id": THREAD},
                },
            )
            async for _chunk in stream:
                pass

    @staticmethod
    def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        records: list[logging.LogRecord] = []
        for record in caplog.records:
            if record.name not in TRACE_LOGGERS:
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
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_chat(provider, ScenarioName.THINKING_ANSWER)

        if self._complaints(caplog) != []:
            raise AssertionError("self._complaints(caplog) == []")
        if not (
            self._heads(caplog)
            == [
                "llm request started",
                "llm first token",
                "llm thinking started",
                "llm thinking finished",
                "llm answer started",
                "llm answer finished",
                "llm request finished",
            ]
        ):
            raise AssertionError('self._heads(caplog) == [ "llm request started", "ll…')

    async def test_stage_lines_carry_size_and_duration(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_chat(provider, ScenarioName.THINKING_ANSWER)

        finished = next(
            line for line in self._lines(caplog) if line.startswith("llm thinking fin")
        )
        reasoning = ScenarioBook.of(ScenarioName.THINKING_ANSWER).turn(0).reasoning
        expected = f"{len(reasoning)} chars in"
        if expected not in finished:
            raise AssertionError(f"{expected!r} not in {finished!r}")
        if not (finished.endswith("ms")):
            raise AssertionError('finished.endswith("ms")')

    async def test_lines_are_formatted_with_user_and_thread(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Тот же форматтер, что и у приложения: метка обязана попасть в строку."""
        await self._stream_chat(provider, ScenarioName.ANSWER)

        spec = LOGGING_CONFIG["formatters"]["default"]
        formatter = DefaultFormatter(fmt=spec["fmt"], use_colors=False)
        formatted: list[str] = []
        for record in self._records(caplog):
            formatted.append(formatter.format(record))

        if self._complaints(caplog) != []:
            raise AssertionError("self._complaints(caplog) == []")
        if not (formatted):
            raise AssertionError("formatted")
        for line in formatted:
            if f"[{MARK}]" not in line:
                raise AssertionError('f"[{MARK}]" in line')

    async def test_mark_does_not_leak_after_the_line(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Метка живёт только на время записи: чужие строки её не наследуют."""
        await self._stream_chat(provider, ScenarioName.ANSWER)

        if LogUserMark.current() != "":
            raise AssertionError('LogUserMark.current() == ""')

        record = logging.getLogger("probe").makeRecord(
            "probe", logging.INFO, "f.py", 1, "after the turn", (), None
        )
        if getattr(record, UserLogContext.ATTRIBUTE) != UserLogContext.UNKNOWN:
            raise AssertionError("getattr(record, UserLogContext.ATTRIBUTE) == UserLo…")

    async def test_answer_without_stream_is_logged_as_complete(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        chat = self._chat(provider)
        await chat.ainvoke(
            ScenarioName.THINKING_ANSWER.value, config={"callbacks": [self._log()]}
        )

        if self._complaints(caplog) != []:
            raise AssertionError("self._complaints(caplog) == []")
        if not (
            self._heads(caplog)
            == [
                "llm request started",
                "llm thinking complete",
                "llm answer complete",
                "llm request finished",
            ]
        ):
            raise AssertionError('self._heads(caplog) == [ "llm request started", "ll…')
        if "tokens in=11 out=7" not in self._lines(caplog)[-1]:
            raise AssertionError('"tokens in=11 out=7" in self._lines(caplog)[-1]')

    async def test_tool_call_turn_logs_both_runs_and_the_call(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_agent(provider, ScenarioName.TOOL)

        heads = self._heads(caplog)
        if self._complaints(caplog) != []:
            raise AssertionError("self._complaints(caplog) == []")
        if heads.count("llm request started") != 2:
            raise AssertionError('heads.count("llm request started") == 2')
        if "tool connection_list started" not in heads:
            raise AssertionError('"tool connection_list started" in heads')
        if "tool connection_list finished" not in heads:
            raise AssertionError('"tool connection_list finished" in heads')
        if not (
            heads.index("tool connection_list started")
            > heads.index("llm request finished")
        ):
            raise AssertionError('heads.index("tool connection_list started") > hea…')

    async def test_tool_line_reports_call_id_and_duration(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._stream_agent(provider, ScenarioName.TOOL)

        finished = next(
            line
            for line in self._lines(caplog)
            if line.startswith("tool connection_list finished")
        )
        if "call=call_connection_list" not in finished:
            raise AssertionError('"call=call_connection_list" in finished')
        if "output=11 chars" not in finished:
            raise AssertionError('"output=11 chars" in finished')

    async def test_failed_tool_is_logged_as_failed(
        self, provider: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Инструмент падает по-настоящему: его отказ едет модели итогом
        вызова, а журнал называет вызов упавшим и причину."""
        await self._stream_agent(provider, ScenarioName.TOOL_ERROR)

        failed = [
            line
            for line in self._lines(caplog)
            if line.startswith("tool send_file failed")
        ]
        if len(failed) != 1:
            raise AssertionError(f"одна строка об упавшем вызове: {failed}")
        if "file not found" not in failed[0]:
            raise AssertionError(f"строка называет причину: {failed[0]!r}")
