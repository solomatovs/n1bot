"""Исполнение одного запуска workflow: стадии по автомату, задачи — штатные вызовы.

Каждая задача — `tool.ainvoke(ToolCall)` через всю цепочку хуков реестра:
роли, журнал под `scope.id` запуска, `intent`, ошибка результатом. Задачи
готовой стадии стартуют вместе; рёбра-значения подставляют `llm_text`
результата источника в аргументы приёмника. Потоковая стадия (задачи,
связанные fd-рёбрами) поднимается цепочкой: каналы задач отдаются слотам
(PipelineSlot), рёбра соединяет splice — данные текут между процессами
через ядро, минуя хост. Остановка — отменой запуска: работающие задачи
снимаются, незапущенные помечает автомат.

Ошибки:
WorkflowRunError — план завис без готовых стадий и работающих задач.
ToolUnavailableError, ToolContractError — от исполнителя: инструмента нет
    или он нарушил контракт.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, ClassVar

from langchain_core.messages import ToolCall

from boba.identity.context import CallContext
from boba.identity.run import RunRegistry
from boba.toolkit.calls import CallIdPrefix, ToolIntent
from boba.toolkit.chain import (
    CallRelay,
    ChainCheck,
    ChainMismatchError,
    NodeSlot,
    PipelineSlot,
)
from boba.toolkit.failure import FailureText, InvokeErrorKind
from boba.toolkit.ports import StreamSpec, ToolStreamSpecs
from boba.toolkit.result import ErrorResult, ToolResult
from boba.toolrun.invoke import InvokeReply, ToolInvoker
from boba.toolrun.streams import StreamPumps
from boba.workflow import (
    RunState,
    Stage,
    TaskStatus,
    WorkflowGraph,
    WorkflowPlan,
)
from boba.workflow.ports import RunSink
from boba.workflow.records import TaskOutcome, WorkflowRunError

__all__ = ["WorkflowRunner"]

logger = logging.getLogger(__name__)


class ReplyOutcome:
    """Сборка TaskOutcome из ответа исполнителя инструментов."""

    @classmethod
    def of_reply(cls, reply: InvokeReply) -> TaskOutcome:
        if reply.ok:
            return TaskOutcome(TaskStatus.DONE, reply.result, "")

        return TaskOutcome(TaskStatus.FAILED, reply.result, reply.error_text)

    @classmethod
    def of_failure(cls, error: Exception) -> TaskOutcome:
        text = FailureText.of(error)
        result = ErrorResult(message=text, error_kind=InvokeErrorKind.CRASHED)
        return TaskOutcome(TaskStatus.FAILED, result, text)

    @classmethod
    def stopped(cls) -> TaskOutcome:
        result = ErrorResult(message="stopped", error_kind=InvokeErrorKind.STOPPED)
        return TaskOutcome(TaskStatus.STOPPED, result, "stopped")


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
        pumps = StreamPumps(sink)

        try:
            with (
                RunRegistry.open(context, on_stream=pumps.opened),
                context.cancellation.abort_with(session.abort),
            ):
                return await session.drive()
        finally:
            await pumps.close()

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
        self._slots: dict[str, NodeSlot] = {}
        self._wires: list[asyncio.Task[None]] = []
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
            msg = (
                f"workflow {self._graph.spec.name!r}: the plan is stuck, "
                "no task runs and none is ready while some are still pending"
            )
            raise WorkflowRunError(msg)

        await asyncio.gather(*self._wires, return_exceptions=True)

        state = self._plan.snapshot()
        await self._sink.snapshot(state)
        return state, dict(self._results)

    FD_WAIT_SEC: ClassVar[float] = 60.0
    """Сколько ждать дескриптор канала от узла потоковой стадии."""

    def _launch(self, stage: Stage) -> None:
        if stage.streams:
            self._launch_streamed(stage)
            return

        for name in stage.tasks:
            call = self._runner.call_of(self._graph, name, self._args_of(name))
            self._plan.started(name, str(call["id"]), self._runner.clock())
            self._running[name] = asyncio.create_task(
                self._runner.invoker.invoke(call), name=f"workflow:{name}"
            )

    def _launch_streamed(self, stage: Stage) -> None:
        """Стадия по потоковым рёбрам: цепочка задач с разводкой каналов."""
        try:
            order = self._stream_order(stage)
        except ChainMismatchError as exc:
            self._refuse_stage(stage, str(exc))
            return

        slots: dict[str, NodeSlot] = {}
        last = len(order) - 1

        for index, name in enumerate(order):
            slot = NodeSlot(has_upstream=index > 0, has_downstream=index < last)
            slots[name] = slot
            self._slots[name] = slot

            call = self._runner.call_of(self._graph, name, self._args_of(name))
            self._plan.started(name, str(call["id"]), self._runner.clock())

            token = PipelineSlot.set(slot)
            try:
                self._running[name] = asyncio.create_task(
                    self._runner.invoker.invoke(call), name=f"workflow:{name}"
                )
            finally:
                PipelineSlot.reset(token)

        self._wires.append(
            asyncio.create_task(
                self._wire(order, slots), name=f"workflow-stage:{order[0]}"
            )
        )

    def _stream_order(self, stage: Stage) -> list[str]:
        """Порядок цепочки стадии; ветвления и циклы пока не поддержаны.

        Провод несёт один канал на направление, поэтому у задачи не больше
        одного потокового ребра в каждую сторону; стыковка kind'ов
        проверяется декларациями до старта процессов.
        """
        follower = self._followers_of(stage)
        fed = set(follower.values())

        heads: list[str] = []
        for name in stage.tasks:
            if name not in fed:
                heads.append(name)

        if len(heads) != 1:
            msg = (
                f"stage {stage.id}: stream stage must be one linear chain with "
                f"a single head, got {len(heads)} heads {heads}; cycles and "
                "parallel chains are not supported yet"
            )
            raise ChainMismatchError(msg)

        order = [heads[0]]
        while order[-1] in follower:
            order.append(follower[order[-1]])

        if len(order) != len(stage.tasks):
            msg = (
                f"stage {stage.id}: stream stage must be one connected chain, "
                f"the chain from {heads[0]!r} covers {len(order)} of "
                f"{len(stage.tasks)} tasks"
            )
            raise ChainMismatchError(msg)

        specs: list[StreamSpec] = []
        for name in order:
            specs.append(ToolStreamSpecs.of(self._graph.spec.tasks[name].tool))

        for left, right in pairwise(specs):
            ChainCheck.ensure(left, right)

        return order

    @staticmethod
    def _followers_of(stage: Stage) -> dict[str, str]:
        """Кто за кем в цепочке; второе ребро в ту же сторону — отказ."""
        follower: dict[str, str] = {}
        fed: set[str] = set()
        for edge in stage.streams:
            if edge.src.task in follower:
                msg = (
                    f"stage {stage.id}: task {edge.src.task!r} feeds both "
                    f"{follower[edge.src.task]!r} and {edge.dst.task!r}; stream "
                    "fan-out is not supported yet, a task carries at most one "
                    "stream edge per direction"
                )
                raise ChainMismatchError(msg)

            if edge.dst.task in fed:
                msg = (
                    f"stage {stage.id}: task {edge.dst.task!r} is fed by more "
                    "than one stream; stream fan-in is not supported yet, a task "
                    "carries at most one stream edge per direction"
                )
                raise ChainMismatchError(msg)

            follower[edge.src.task] = edge.dst.task
            fed.add(edge.dst.task)

        return follower

    def _refuse_stage(self, stage: Stage, message: str) -> None:
        """Стадия не собирается в цепочку: задачи закрываются отказом."""
        for name in stage.tasks:
            self._plan.started(name, "", self._runner.clock())
            result = ErrorResult(message=message, error_kind=InvokeErrorKind.CRASHED)
            self._results[name] = result
            self._plan.finished(
                name, TaskStatus.FAILED, self._runner.clock(), message, result
            )

    async def _wire(self, order: Sequence[str], slots: Mapping[str, NodeSlot]) -> None:
        """Соединяет рёбра цепочки splice-задачами и ждёт перекачку."""
        relays: list[asyncio.Task[Any]] = []
        try:
            for left, right in pairwise(order):
                source_fd = await asyncio.to_thread(
                    slots[left].take_source_fd, self.FD_WAIT_SEC
                )
                sink_fd = await asyncio.to_thread(
                    slots[right].take_input_fd, self.FD_WAIT_SEC
                )
                relays.append(
                    asyncio.create_task(
                        asyncio.to_thread(CallRelay.splice, source_fd, sink_fd),
                        name=f"workflow-edge:{left}->{right}",
                    )
                )
        except ChainMismatchError as exc:
            logger.warning(
                "workflow %s: wiring of the stream chain %s failed: %s",
                self._graph.spec.name,
                order,
                exc,
            )
            for slot in slots.values():
                slot.abort()

        moved = await asyncio.gather(*relays, return_exceptions=True)
        for pair, stats in zip(pairwise(order), moved, strict=True):
            if isinstance(stats, BaseException):
                logger.warning(
                    "workflow %s: relay on the stream edge %s failed: %s",
                    self._graph.spec.name,
                    pair,
                    stats,
                )
                continue

            logger.info("workflow edge %s: %d bytes moved", pair, stats.bytes)

    async def _stop_now(self) -> None:
        """Отмена самой корутины запуска: работающие задачи снимаем и ждём."""
        self._plan.stop()
        for slot in self._slots.values():
            slot.abort()

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

        msg = (
            f"workflow {self._graph.spec.name!r}: finished asyncio task "
            f"{task.get_name()!r} is not among the running tasks "
            f"{sorted(self._running)}"
        )
        raise WorkflowRunError(msg)

    @staticmethod
    def _outcome_of(task: asyncio.Task[InvokeReply]) -> TaskOutcome:
        if task.cancelled():
            return ReplyOutcome.stopped()

        error = task.exception()
        if error is None:
            return ReplyOutcome.of_reply(task.result())

        if not isinstance(error, Exception):
            raise error

        return ReplyOutcome.of_failure(error)

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
