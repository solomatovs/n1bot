"""Исполнение одного запуска workflow: стадии по автомату, задачи — штатные вызовы.

Каждая задача — `tool.ainvoke(ToolCall)` через всю цепочку хуков реестра:
роли, журнал под `scope.id` запуска, `intent`, ошибка результатом. Задачи
готовой стадии стартуют вместе; рёбра-значения подставляют `llm_text`
результата источника в аргументы приёмника. Остановка — отменой запуска:
работающие задачи снимаются, незапущенные помечает автомат.

Ошибки:
WorkflowRunError — план завис без готовых стадий и работающих задач.
ToolUnavailableError, ToolContractError — от исполнителя: инструмента нет
    или он нарушил контракт.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from langchain_core.messages import ToolCall

from boba.chainlit.agent.invoke import (
    CallIdPrefix,
    InvokeErrorKind,
    InvokeReply,
    ToolInvoker,
)
from boba.chainlit.domain.context import CallContext
from boba.chainlit.domain.run import RunRegistry
from boba.toolkit.calls import ToolIntent
from boba.toolkit.failure import FailureText
from boba.toolkit.result import ErrorResult, ToolResult
from boba.workflow import (
    RunState,
    Stage,
    TaskStatus,
    WorkflowGraph,
    WorkflowPlan,
)

__all__ = ["RunSink", "TaskOutcome", "WorkflowRunError", "WorkflowRunner"]

logger = logging.getLogger(__name__)


class WorkflowRunError(Exception):
    """Раннер не может продолжать: контракт инструментов или автомата нарушен."""


class RunSink(Protocol):
    """Куда уходят снимки состояния по ходу запуска."""

    async def snapshot(self, state: RunState) -> None: ...


class TaskOutcome:
    """Итог задачи: статус для автомата и результат для рёбер и отчёта."""

    def __init__(self, status: TaskStatus, result: ToolResult, error: str) -> None:
        self.status = status
        self.result = result
        self.error = error

    @classmethod
    def of_reply(cls, reply: InvokeReply) -> TaskOutcome:
        if reply.ok:
            return cls(TaskStatus.DONE, reply.result, "")

        return cls(TaskStatus.FAILED, reply.result, reply.error_text)

    @classmethod
    def of_failure(cls, error: Exception) -> TaskOutcome:
        text = FailureText.of(error)
        result = ErrorResult(message=text, error_kind=InvokeErrorKind.CRASHED)
        return cls(TaskStatus.FAILED, result, text)

    @classmethod
    def stopped(cls) -> TaskOutcome:
        result = ErrorResult(message="stopped", error_kind=InvokeErrorKind.STOPPED)
        return cls(TaskStatus.STOPPED, result, "stopped")


class WorkflowRunner:
    """Гоняет план запуска над инструментами исполнителя под контекстом вызова."""

    def __init__(self, invoker: ToolInvoker, clock: Callable[[], datetime]) -> None:
        self._invoker = invoker
        self._clock = clock

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(tz=UTC)

    @property
    def invoker(self) -> ToolInvoker:
        return self._invoker

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    async def run(
        self, graph: WorkflowGraph, context: CallContext, sink: RunSink
    ) -> tuple[RunState, Mapping[str, ToolResult]]:
        """Запуск до конца или остановки; итог — состояние и результаты задач."""
        session = _RunSession(self, graph, context, sink)

        with RunRegistry.open(context), context.cancellation.abort_with(session.abort):
            return await session.drive()

    @staticmethod
    def call_of(graph: WorkflowGraph, task: str, args: Mapping[str, Any]) -> ToolCall:
        """Подпись вызова — из задачи, если задана, иначе «workflow: задача»."""
        spec = graph.spec.tasks[task]
        intent = ToolIntent.of(args)
        if not intent:
            intent = f"{graph.spec.name}: {task}"

        return ToolInvoker.call(
            spec.tool, ToolIntent.without(args), intent, CallIdPrefix.WORKFLOW
        )


class _RunSession:
    """Состояние одного прогона: план, работающие задачи, результаты."""

    def __init__(
        self,
        runner: WorkflowRunner,
        graph: WorkflowGraph,
        context: CallContext,
        sink: RunSink,
    ) -> None:
        self._runner = runner
        self._graph = graph
        self._context = context
        self._sink = sink
        self._plan = WorkflowPlan(graph)
        self._running: dict[str, asyncio.Task[InvokeReply]] = {}
        self._results: dict[str, ToolResult] = {}
        self._loop = asyncio.get_running_loop()

    def abort(self) -> None:
        """Прерыватель отмены: зовут из любого потока, задачи снимаем через loop."""
        self._loop.call_soon_threadsafe(self._cancel_running)

    def _cancel_running(self) -> None:
        for task in self._running.values():
            task.cancel()

    async def drive(self) -> tuple[RunState, Mapping[str, ToolResult]]:
        await self._sink.snapshot(self._plan.snapshot())

        while True:
            if self._context.cancellation.cancelled:
                self._plan.stop()

            launched = self._plan.ready()
            for stage in launched:
                self._launch(stage)

            if launched:
                await self._sink.snapshot(self._plan.snapshot())

            if not self._running:
                break

            try:
                await self._settle_some()
            except asyncio.CancelledError:
                await self._stop_now()

            await self._sink.snapshot(self._plan.snapshot())

        if not self._plan.done:
            raise WorkflowRunError(
                "the plan is stuck: nothing runs and nothing is ready"
            )

        state = self._plan.snapshot()
        await self._sink.snapshot(state)
        return state, dict(self._results)

    def _launch(self, stage: Stage) -> None:
        for name in stage.tasks:
            call = self._runner.call_of(self._graph, name, self._args_of(name))
            self._plan.started(name, str(call["id"]), self._runner.clock())
            self._running[name] = asyncio.create_task(
                self._runner.invoker.invoke(call), name=f"workflow:{name}"
            )

    async def _stop_now(self) -> None:
        """Отмена самой корутины запуска: работающие задачи снимаем и ждём."""
        self._plan.stop()
        self._cancel_running()
        done, _ = await asyncio.wait(self._running.values())
        self._settle(done)

        current = asyncio.current_task()
        if current is not None:
            current.uncancel()

    async def _settle_some(self) -> None:
        done, _ = await asyncio.wait(
            self._running.values(), return_when=asyncio.FIRST_COMPLETED
        )
        self._settle(done)

    def _settle(self, done: set[asyncio.Task[InvokeReply]]) -> None:
        for task in done:
            name = self._name_of(task)
            del self._running[name]
            outcome = self._outcome_of(task)
            self._results[name] = outcome.result
            self._plan.finished(
                name,
                outcome.status,
                self._runner.clock(),
                outcome.error,
                outcome.result,
            )
            self._log(name, outcome)

    def _name_of(self, task: asyncio.Task[InvokeReply]) -> str:
        for name, running in self._running.items():
            if running is task:
                return name

        raise WorkflowRunError("a finished task is not among the running ones")

    @staticmethod
    def _outcome_of(task: asyncio.Task[InvokeReply]) -> TaskOutcome:
        if task.cancelled():
            return TaskOutcome.stopped()

        error = task.exception()
        if error is None:
            return TaskOutcome.of_reply(task.result())

        if not isinstance(error, Exception):
            raise error

        return TaskOutcome.of_failure(error)

    def _args_of(self, name: str) -> dict[str, Any]:
        """Аргументы задачи: спека плюс тексты результатов по привязкам графа."""
        texts: dict[str, str] = {}
        for source in self._graph.sources_of(name):
            texts[source] = self._results[source].llm_text()

        return self._graph.args_of(name, texts)

    def _log(self, name: str, outcome: TaskOutcome) -> None:
        if outcome.status is TaskStatus.DONE:
            logger.info("workflow %s: task %s done", self._graph.spec.name, name)
            return

        logger.warning(
            "workflow %s: task %s %s: %s",
            self._graph.spec.name,
            name,
            outcome.status.value,
            outcome.error,
        )
