"""Логи вокруг вызова инструмента и причина падения песочницы."""

from __future__ import annotations

import asyncio
import logging

import pytest
from langchain_core.tools import StructuredTool, tool
from typing import Any

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.run_log import StreamSource, ToolRunLogger
from boba.sandbox.process_runner import RunResult
from boba.sandbox.runner import SandboxRunner
from boba.toolkit.result import TextResult, ToolArtifact, pack_result
from boba.toolkit.stream import ToolCallContext

LOGGER_NAME = "boba.chainlit.agent.toolrun.run_log"
RUNNER_LOGGER_NAME = "boba.sandbox.runner"


def no_streams(tool: str, call_id: str) -> None:
    return None


NO_STREAMS: StreamSource = no_streams


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
        ToolRunLogger.guard_all([tool], NO_STREAMS)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            if tool.func is None:
                raise AssertionError("tool.func is not None")
            tool.func(query="звук")
        messages = [r.getMessage() for r in caplog.records]
        start_prefix = "tool[probe]: start args=query='звук'"
        if not (any(m.startswith(start_prefix) for m in messages)):
            raise AssertionError("any(m.startswith(start_prefix) for m in messages)")
        if not (any(m.startswith("tool[probe]: ok in ") for m in messages)):
            raise AssertionError('any(m.startswith("tool[probe]: ok in ") for m in me…')

    def test_failure_logged_and_reraised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(query: str) -> str:
            msg = "нет соединения"
            raise RuntimeError(msg)

        tool = self._tool(boom)
        ToolRunLogger.guard_all([tool], NO_STREAMS)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            if tool.func is None:
                raise AssertionError("tool.func is not None")
            with pytest.raises(RuntimeError):
                tool.func(query="q")
        warning = [r for r in caplog.records if r.levelno == logging.WARNING]
        if len(warning) != 1:
            raise AssertionError("len(warning) == 1")
        if "tool[probe]: failed in" not in warning[0].getMessage():
            raise AssertionError('"tool[probe]: failed in" in warning[0].getMessage()')
        if "RuntimeError: нет соединения" not in warning[0].getMessage():
            raise AssertionError('"RuntimeError: нет соединения" in warning[0].getMes…')

    def test_context_set_inside_and_reset_after(self) -> None:
        seen: list[str] = []

        def probe(query: str) -> str:
            seen.append(ToolCallContext.name())
            return "ok"

        tool = self._tool(probe)
        ToolRunLogger.guard_all([tool], NO_STREAMS)
        if tool.func is None:
            raise AssertionError("tool.func is not None")
        tool.func(query="q")
        if seen != ["probe"]:
            raise AssertionError('seen == ["probe"]')
        if ToolCallContext.get() is not None:
            raise AssertionError("ToolCallContext.get() is None")

    def test_async_tool_wrapped(self, caplog: pytest.LogCaptureFixture) -> None:
        async def probe(query: str) -> str:
            return ToolCallContext.name()

        tool = self._tool(lambda query: "sync", probe)
        ToolRunLogger.guard_all([tool], NO_STREAMS)

        async def invoke() -> object:
            if tool.coroutine is None:
                raise AssertionError("tool.coroutine is not None")
            return await tool.coroutine(query="q")

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            result = asyncio.run(invoke())
        if result != "probe":
            raise AssertionError('result == "probe"')
        messages = [r.getMessage() for r in caplog.records]
        if not (any(m.startswith("tool[probe]: ok in ") for m in messages)):
            raise AssertionError('any(m.startswith("tool[probe]: ok in ") for m in me…')

    def test_args_render_truncated(self) -> None:
        rendered = ToolRunLogger._render_args((), {"query": "x" * 1000})
        if len(rendered) != ToolRunLogger.ARGS_LIMIT + 1:
            raise AssertionError("len(rendered) == ToolRunLogger.ARGS_LIMIT + 1")
        if not (rendered.endswith("…")):
            raise AssertionError('rendered.endswith("…")')


class TestSandboxFailureLog:
    @staticmethod
    def _result(rc: int, stderr: str, stdout: str = "", timed_out: bool = False):
        return RunResult(
            exit_code=rc,
            stdout=stdout,
            stderr=stderr,
            duration_ms=42,
            timed_out=timed_out,
        )

    def test_stderr_tail_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            SandboxRunner._log_failure("conf", self._result(1, "Traceback: boom\n"))
        message = caplog.records[0].getMessage()
        if "sandbox[conf]: failed (rc=1)" not in message:
            raise AssertionError('"sandbox[conf]: failed (rc=1)" in message')
        if "Traceback: boom" not in message:
            raise AssertionError('"Traceback: boom" in message')

    def test_stdout_used_when_stderr_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            SandboxRunner._log_failure("conf", self._result(2, "", "partial out"))
        if "partial out" not in caplog.records[0].getMessage():
            raise AssertionError('"partial out" in caplog.records[0].getMessage()')

    def test_no_output_marker(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            SandboxRunner._log_failure("conf", self._result(1, ""))
        if "<no output>" not in caplog.records[0].getMessage():
            raise AssertionError('"<no output>" in caplog.records[0].getMessage()')

    def test_timed_out_reason(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER_NAME):
            SandboxRunner._log_failure("conf", self._result(-9, "", timed_out=True))
        if "timed out after 42ms" not in caplog.records[0].getMessage():
            raise AssertionError('"timed out after 42ms" in caplog.records[0].getMess…')

    def test_tail_truncates_long_output(self) -> None:
        tail = SandboxRunner._tail("x" * (SandboxRunner.FAIL_TAIL_CHARS + 100))
        if len(tail) != SandboxRunner.FAIL_TAIL_CHARS + 1:
            raise AssertionError("len(tail) == SandboxRunner.FAIL_TAIL_CHARS + 1")
        if not (tail.startswith("…")):
            raise AssertionError('tail.startswith("…")')


class TestElapsedInResult:
    """Обвязка запуска кладёт время вызова в артефакт, а не только в лог."""

    @pytest.mark.anyio
    async def test_elapsed_is_recorded(self) -> None:
        @tool(response_format="content_and_artifact")
        async def slow_probe(query: str) -> tuple[str, Any]:
            """Инструмент, который заметно работает."""
            await asyncio.sleep(0.05)
            return pack_result(TextResult(text=f"found {query}"))

        ToolCallIdField.attach_all([slow_probe])
        ToolRunLogger.guard_all([slow_probe], NO_STREAMS)

        message = await slow_probe.ainvoke(
            {"name": "slow_probe", "args": {"query": "x"}, "id": "c1", "type": "tool_call"}
        )
        result = ToolArtifact.revive(message.artifact)

        if not isinstance(result, TextResult):
            raise AssertionError(f"артефакт разобран: {result}")

        if result.elapsed_ms < 50:
            raise AssertionError(f"время вызова не проставлено: {result.elapsed_ms}")

    @pytest.mark.anyio
    async def test_foreign_return_is_untouched(self) -> None:
        """Инструмент вернул не пару content/artifact — обвязка не вмешивается."""
        @tool
        async def plain_probe(query: str) -> str:
            """Инструмент со свободным ответом."""
            return f"plain {query}"

        ToolCallIdField.attach_all([plain_probe])
        ToolRunLogger.guard_all([plain_probe], NO_STREAMS)

        message = await plain_probe.ainvoke(
            {"name": "plain_probe", "args": {"query": "x"}, "id": "c2", "type": "tool_call"}
        )
        if message.content != "plain x":
            raise AssertionError(message.content)
