"""Логи вокруг вызова инструмента и причина падения песочницы."""

from __future__ import annotations

import asyncio
import logging

import pytest
from langchain_core.tools import StructuredTool

from boba.chainlit.agent.tools.run_log import ToolRunLogger
from boba.sandbox.diagnostics import FailureFacts
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import WorkflowRunner

LOGGER_NAME = "boba.chainlit.agent.tools.run_log"
RUNNER_LOGGER_NAME = "boba.sandbox.workflow"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestToolRunLogger:
    @staticmethod
    def _tool(func, coroutine=None) -> StructuredTool:
        return StructuredTool.from_function(
            func=func, coroutine=coroutine, name="probe", description="probe"
        )

    def test_success_logs_start_and_ok(self, caplog: pytest.LogCaptureFixture) -> None:
        tool = self._tool(lambda query: "done")
        ToolRunLogger.guard_all([tool])
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            assert tool.func is not None
            tool.func(query="звук")
        messages = [r.getMessage() for r in caplog.records]
        start_prefix = "tool[probe]: start args=query='звук'"
        assert any(m.startswith(start_prefix) for m in messages)
        assert any(m.startswith("tool[probe]: ok in ") for m in messages)

    def test_failure_logged_and_reraised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(query: str) -> str:
            msg = "нет соединения"
            raise RuntimeError(msg)

        tool = self._tool(boom)
        ToolRunLogger.guard_all([tool])
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            assert tool.func is not None
            with pytest.raises(RuntimeError):
                tool.func(query="q")
        warning = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning) == 1
        assert "tool[probe]: failed in" in warning[0].getMessage()
        assert "RuntimeError: нет соединения" in warning[0].getMessage()

    def test_context_set_inside_and_reset_after(self) -> None:
        seen: list[str] = []

        def probe(query: str) -> str:
            seen.append(ToolCallContext.get())
            return "ok"

        tool = self._tool(probe)
        ToolRunLogger.guard_all([tool])
        assert tool.func is not None
        tool.func(query="q")
        assert seen == ["probe"]
        assert ToolCallContext.get() == ""

    def test_async_tool_wrapped(self, caplog: pytest.LogCaptureFixture) -> None:
        async def probe(query: str) -> str:
            return ToolCallContext.get()

        tool = self._tool(lambda query: "sync", probe)
        ToolRunLogger.guard_all([tool])

        async def invoke() -> object:
            assert tool.coroutine is not None
            return await tool.coroutine(query="q")

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            result = asyncio.run(invoke())
        assert result == "probe"
        messages = [r.getMessage() for r in caplog.records]
        assert any(m.startswith("tool[probe]: ok in ") for m in messages)

    def test_args_render_truncated(self) -> None:
        rendered = ToolRunLogger._render_args((), {"query": "x" * 1000})
        assert len(rendered) == ToolRunLogger.ARGS_LIMIT + 1
        assert rendered.endswith("…")


class TestSandboxFailureLog:
    DURATION_MS = 42

    @staticmethod
    def _facts(rc: int, stderr: str, timed_out: bool = False) -> FailureFacts:
        return FailureFacts(exit_code=rc, timed_out=timed_out, stderr_tail=stderr)

    def test_stderr_tail_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            WorkflowRunner._log_failure(
                "conf", self._facts(1, "Traceback: boom\n"), self.DURATION_MS, ""
            )
        message = caplog.records[0].getMessage()
        assert "workflow stage conf: failed (rc=1)" in message
        assert "Traceback: boom" in message

    def test_no_output_marker(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            WorkflowRunner._log_failure("conf", self._facts(1, ""), self.DURATION_MS, "")
        assert "<no output>" in caplog.records[0].getMessage()

    def test_timed_out_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            WorkflowRunner._log_failure(
                "conf", self._facts(-9, "", timed_out=True), self.DURATION_MS, ""
            )
        assert "timed out after 42ms" in caplog.records[0].getMessage()

    def test_diagnostic_joins_the_tail(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            WorkflowRunner._log_failure(
                "conf", self._facts(137, "killed"), self.DURATION_MS, "memory limit"
            )
        assert "memory limit" in caplog.records[0].getMessage()

    def test_tail_truncates_long_output(self) -> None:
        tail = WorkflowRunner._tail("x" * (WorkflowRunner.FAIL_TAIL_CHARS + 100))
        assert len(tail) == WorkflowRunner.FAIL_TAIL_CHARS + 1
        assert tail.startswith("…")
