"""Логи вокруг вызова инструмента, адрес вызова и причина падения песочницы."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

import pytest
from chainlit.context import init_http_context
from chainlit.user import PersistedUser
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool

from boba.chainlit.agent.tools.run_log import ToolRunLogger
from boba.sandbox.diagnostics import FailureFacts
from boba.sandbox.runner import ToolCallContext
from boba.sandbox.workflow import WorkflowRunner
from boba.stand.journal import CallStand

LOGGER_NAME = "boba.chainlit.agent.tools.run_log"
RUNNER_LOGGER_NAME = "boba.sandbox.workflow"

USER_ID = "7"
THREAD_ID = "th-run-log"
CALL_ID = "call_probe_1"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Сессии по умолчанию нет: её заводят тесты адреса вызова."""


class TestToolRunLogger:
    @staticmethod
    def _tool(func, coroutine=None) -> StructuredTool:
        return StructuredTool.from_function(
            func=func, coroutine=coroutine, name="probe", description="probe"
        )

    @staticmethod
    def _call(args: dict[str, Any], call_id: str = CALL_ID) -> dict[str, Any]:
        """Полный tool call: id вызова доезжает до обвязки только с ним."""
        return {"name": "probe", "args": args, "id": call_id, "type": "tool_call"}

    @staticmethod
    def _in_session(action) -> Any:
        """Действие внутри сессии chainlit: контекст живёт в её event loop."""

        async def run() -> Any:
            user = PersistedUser(
                id=USER_ID, identifier="tester", createdAt="2026-01-01T00:00:00Z"
            )
            init_http_context(user=user, thread_id=THREAD_ID)
            return action()

        return asyncio.run(run())

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

    def test_call_address_set_inside_and_reset_after(self) -> None:
        seen: list[ToolCallContext] = []

        def probe(query: str) -> str:
            seen.append(ToolCallContext.current())
            return "ok"

        tool = self._tool(probe)
        ToolRunLogger.guard_all([tool])
        self._in_session(lambda: tool.invoke(self._call({"query": "q"})))

        assert len(seen) == 1
        assert seen[0].user_id == USER_ID
        assert seen[0].thread_id == THREAD_ID
        assert seen[0].call_id == CALL_ID
        assert seen[0].tool == "probe"
        # адрес снят: снаружи снова контекст стенда из conftest
        assert ToolCallContext.current().tool == CallStand.TOOL

    def test_call_id_hidden_from_model(self) -> None:
        tool = self._tool(lambda query: "done")
        ToolRunLogger.guard_all([tool])

        # id вызова доезжает до обвязки (соседние тесты), а модель его не видит
        assert "tool_call_id" not in tool.args

    def test_own_call_id_field_left_to_the_tool(self) -> None:
        seen: list[str] = []

        def probe(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
            seen.append(tool_call_id)
            return "ok"

        tool = self._tool(probe)
        ToolRunLogger.guard_all([tool])
        self._in_session(lambda: tool.invoke(self._call({"query": "q"})))

        assert seen == [CALL_ID]

    def test_call_outside_session_keeps_caller_address(self) -> None:
        seen: list[str] = []

        def probe(query: str) -> str:
            seen.append(ToolCallContext.current().tool)
            return "ok"

        tool = self._tool(probe)
        ToolRunLogger.guard_all([tool])
        tool.invoke(self._call({"query": "q"}))

        assert seen == [CallStand.TOOL]

    def test_async_tool_wrapped(self, caplog: pytest.LogCaptureFixture) -> None:
        async def probe(query: str) -> str:
            return ToolCallContext.current().call_id

        tool = self._tool(lambda query: "sync", probe)
        ToolRunLogger.guard_all([tool])

        async def invoke() -> object:
            user = PersistedUser(
                id=USER_ID, identifier="tester", createdAt="2026-01-01T00:00:00Z"
            )
            init_http_context(user=user, thread_id=THREAD_ID)
            return await tool.ainvoke(self._call({"query": "q"}))

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            message = asyncio.run(invoke())

        assert isinstance(message, ToolMessage)
        assert message.content == CALL_ID
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
            WorkflowRunner._log_failure(
                "conf", self._facts(1, ""), self.DURATION_MS, ""
            )
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
