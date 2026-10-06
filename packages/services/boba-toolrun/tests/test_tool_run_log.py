"""Логи вокруг вызова инструмента и причина падения песочницы."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from boba.identity.context import CallContexts, LlmInitiator
from boba.identity.run import Runs
from boba.sandbox.runner import FailureLog
from boba.stand.toolstand import ProbeTools
from boba.stand_core.context import CallStand
from boba.toolkit.chain import CallAmbient
from boba.toolkit.facade import NotLogged, ToolFacadeError, tool
from boba.toolkit.launcher import RunResult
from boba.toolkit.result import MarkdownResult
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import HostedTool, ToolHosting
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.streams import CallJournals

LOGGER_NAME = "boba.toolrun.run_log"


TAIL_CHARS = 2000
"""Хвост вывода в сообщении об ошибке; в проде значение из профиля."""


class QueryArgs(BaseModel):
    """Схема пробного инструмента: один аргумент модели."""

    query: str


class TestToolRunLogger:
    @staticmethod
    def _logged(tool: HostedTool, contexts: CallContexts) -> HostedTool:
        ToolRunLogger(
            CallJournals(None, Runs(contexts)), contexts, CallAmbient()
        ).guard_all([tool])

        return tool

    def test_success_logs_start_and_ok(self, caplog: pytest.LogCaptureFixture) -> None:
        async def done(query: str) -> MarkdownResult:
            return MarkdownResult(text="done")

        probes = ProbeTools()
        tool = self._logged(probes.hosted("probe", QueryArgs, done), CallContexts())
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            asyncio.run(probes.call(tool, "c1", {"query": "звук"}))
        messages = [r.getMessage() for r in caplog.records]
        start_prefix = "tool[probe]: start args=query='звук'"
        if not (any(m.startswith(start_prefix) for m in messages)):
            raise AssertionError("any(m.startswith(start_prefix) for m in messages)")
        if not (any(m.startswith("tool[probe]: ok in ") for m in messages)):
            raise AssertionError('any(m.startswith("tool[probe]: ok in ") for m in me…')

    def test_not_logged_argument_is_not_written(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Аргумент, помеченный в схеме NotLogged, в лог не пишется вовсе."""

        class Args(BaseModel):
            connection: Annotated[str, NotLogged()]
            sql: str

        async def done(connection: str, sql: str) -> MarkdownResult:
            return MarkdownResult(text="done")

        probes = ProbeTools()
        tool = self._logged(probes.hosted("probe", Args, done), CallContexts())

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            sent = {"connection": "not_logged-value", "sql": "select 1"}
            asyncio.run(probes.call(tool, "c1", sent))

        started = [r.getMessage() for r in caplog.records if "start args" in r.message]
        if "connection" in started[0]:
            raise AssertionError(f"помеченный аргумент попал в лог: {started[0]}")
        if "not_logged-value" in started[0]:
            raise AssertionError(f"значение попало в лог: {started[0]}")
        if "sql='select 1'" not in started[0]:
            raise AssertionError(f"остальные аргументы как есть: {started[0]}")

    def test_failure_logged_and_reraised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def boom(query: str) -> MarkdownResult:
            msg = "нет соединения"
            raise RuntimeError(msg)

        probes = ProbeTools()
        tool = self._logged(probes.hosted("probe", QueryArgs, boom), CallContexts())
        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            pytest.raises(RuntimeError),
        ):
            asyncio.run(probes.call(tool, "c1", {"query": "q"}))
        warning = [r for r in caplog.records if r.levelno == logging.WARNING]
        if len(warning) != 1:
            raise AssertionError("len(warning) == 1")
        if "tool[probe]: failed in" not in warning[0].getMessage():
            raise AssertionError('"tool[probe]: failed in" in warning[0].getMessage()')
        if "RuntimeError: нет соединения" not in warning[0].getMessage():
            raise AssertionError('"RuntimeError: нет соединения" in warning[0].getMes…')

    def test_call_context_is_the_tool_call_inside_and_restored_after(
        self, call_stand: CallStand
    ) -> None:
        """На время вызова стоит контекст вызова моделью; после — прежний."""
        contexts = call_stand.contexts
        inside: list[str] = []

        async def probe(query: str) -> MarkdownResult:
            initiator = contexts.current().initiator
            if isinstance(initiator, LlmInitiator):
                inside.append(initiator.tool_call_id)

            return MarkdownResult(text="ok")

        probes = ProbeTools()
        tool = probes.hosted("probe", QueryArgs, probe)
        CallFields().attach_all([tool])
        self._logged(tool, contexts)

        outer = call_stand.context("t1")
        with call_stand.applied(outer):
            asyncio.run(probes.call(tool, "call-1", {"query": "q"}))
            if contexts.current() is not outer:
                raise AssertionError(contexts.current())

        if inside != ["call-1"]:
            raise AssertionError(inside)

    def test_result_passes_through(self, caplog: pytest.LogCaptureFixture) -> None:
        async def probe(query: str) -> MarkdownResult:
            return MarkdownResult(text="probe")

        probes = ProbeTools()
        tool = self._logged(probes.hosted("probe", QueryArgs, probe), CallContexts())

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            outcome = asyncio.run(probes.call(tool, "c1", {"query": "q"}))
        if outcome.content != "probe":
            raise AssertionError(outcome.content)
        messages = [r.getMessage() for r in caplog.records]
        if not (any(m.startswith("tool[probe]: ok in ") for m in messages)):
            raise AssertionError('any(m.startswith("tool[probe]: ok in ") for m in me…')

    def test_args_render_truncated(self) -> None:
        rendered = ToolRunLogger._render_args({"query": "x" * 1000}, frozenset())
        if len(rendered) != ToolRunLogger.ARGS_LIMIT + 1:
            raise AssertionError("len(rendered) == ToolRunLogger.ARGS_LIMIT + 1")
        if not (rendered.endswith("…")):
            raise AssertionError('rendered.endswith("…")')


class TestSandboxFailureLog:
    """Описание падения для журнала: причина плюс хвост вывода."""

    @staticmethod
    def _result(rc: int, stderr: str, stdout: str = "", timed_out: bool = False):
        return RunResult(
            exit_code=rc,
            stdout=stdout,
            stderr=stderr,
            duration_ms=42,
            timed_out=timed_out,
        )

    def test_stderr_tail_logged(self) -> None:
        message = FailureLog.describe(self._result(1, "Traceback: boom\n"), TAIL_CHARS)
        if "failed (rc=1)" not in message:
            raise AssertionError('"failed (rc=1)" in message')
        if "Traceback: boom" not in message:
            raise AssertionError('"Traceback: boom" in message')

    def test_stdout_used_when_stderr_empty(self) -> None:
        message = FailureLog.describe(self._result(2, "", "partial out"), TAIL_CHARS)
        if "partial out" not in message:
            raise AssertionError('"partial out" in message')

    def test_no_output_marker(self) -> None:
        message = FailureLog.describe(self._result(1, ""), TAIL_CHARS)
        if FailureLog.NO_OUTPUT not in message:
            raise AssertionError("FailureLog.NO_OUTPUT in message")

    def test_timed_out_reason(self) -> None:
        message = FailureLog.describe(self._result(-9, "", timed_out=True), TAIL_CHARS)
        if "timed out after 42ms" not in message:
            raise AssertionError('"timed out after 42ms" in message')

    def test_tail_truncates_long_output(self) -> None:
        tail = FailureLog.tail("x" * (TAIL_CHARS + 100), TAIL_CHARS)
        if len(tail) != TAIL_CHARS + 1:
            raise AssertionError("len(tail) == TAIL_CHARS + 1")
        if not (tail.startswith("…")):
            raise AssertionError('tail.startswith("…")')


class TestElapsedInResult:
    """Обвязка запуска кладёт время вызова в артефакт, а не только в лог."""

    @staticmethod
    def _guarded(probe: HostedTool) -> HostedTool:
        CallFields().attach_all([probe])
        contexts = CallContexts()
        ToolRunLogger(
            CallJournals(None, Runs(contexts)), contexts, CallAmbient()
        ).guard_all([probe])

        return probe

    @pytest.mark.anyio
    async def test_elapsed_is_recorded(self) -> None:
        async def slow_probe(query: str) -> MarkdownResult:
            await asyncio.sleep(0.05)
            return MarkdownResult(text=f"found {query}")

        probes = ProbeTools()
        probe = self._guarded(probes.hosted("slow_probe", QueryArgs, slow_probe))

        outcome = await probes.call(probe, "c1", {"query": "x"})
        result = outcome.artifact

        if not isinstance(result, MarkdownResult):
            raise AssertionError(f"артефакт разобран: {result}")

        if result.elapsed_ms < 50:
            raise AssertionError(f"время вызова не проставлено: {result.elapsed_ms}")

    @pytest.mark.anyio
    async def test_foreign_return_is_refused(self) -> None:
        """Тело автора вернуло не модель результата — вызов отвергнут с
        именем инструмента, обвязка чужое значение не разбирает."""

        @tool
        async def plain_probe(query: str) -> MarkdownResult:
            """Проба, чьё тело нарушает контракт результата."""
            foreign: Any = f"plain {query}"

            return foreign

        probe = self._guarded(ToolHosting().hosted(plain_probe))

        with pytest.raises(ToolFacadeError) as refused:
            await probe.run("c2", {"query": "x"})

        expected = "tool 'plain_probe' must return a ToolResultBase model, got str"
        if str(refused.value) != expected:
            raise AssertionError(str(refused.value))
