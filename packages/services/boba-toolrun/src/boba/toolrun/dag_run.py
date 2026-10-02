"""Исполнитель DAG потоковых вызовов.

DagRunner принимает описание DAG (boba.toolkit.dag) и обёрнутые инструменты
реестра, строит план каналов и группу «все или никто» (boba.toolkit.chain) и
сам запускает каждый узел: вызов инструмента идёт через его обвязки —
права, журнал, отмена, упаковка ошибок — под ручкой группы в PipelineSlot.
Узел, не дошедший до запуска (права, аргументы), срывает группу сразу. Итог
— DagOutcome: результат каждого узла. Один исполнитель служит чату (узлы —
вызовы ответа модели) и запуску без модели.

Ошибки:
StreamPlanError — описание не переводится в план: поле порта, pipe_bytes,
    правила графа.
DagRunError — узел зовёт инструмент, которого нет среди переданных, либо
    инструмент ответил не сообщением с результатом семейства ToolResultBase.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

from boba.toolkit.chain import PipelineSlot, StreamGroupRun, StreamTimings
from boba.toolkit.dag import DagNode, DagPlanner, DagSpec
from boba.toolkit.failure import FailurePacker
from boba.toolkit.ports import ToolStreamSpecs
from boba.toolkit.result import (
    FailureResult,
    GroupFailureResult,
    ToolResult,
    ToolResultBase,
)

__all__ = ["DagHandle", "DagOutcome", "DagRunError", "DagRunner", "NodeOutcome"]

logger = logging.getLogger(__name__)


class DagRunError(RuntimeError):
    """Узел DAG нельзя исполнить: инструмента нет либо его ответ не результат."""


class NodeOutcome(BaseModel):
    """Итог узла: текст для модели и результат семейства; сбой — FailureResult."""

    model_config = ConfigDict(frozen=True)

    key: str
    tool: str
    content: str
    artifact: ToolResult

    def failed(self) -> bool:
        return isinstance(self.artifact, FailureResult)


class DagOutcome(BaseModel):
    """Итог DAG: итоги узлов в порядке описания."""

    model_config = ConfigDict(frozen=True)

    dag: str
    version: int
    nodes: Sequence[NodeOutcome]

    def ok(self) -> bool:
        return not self.failures()

    def failures(self) -> tuple[NodeOutcome, ...]:
        return tuple(self._failed())

    def node(self, key: str) -> NodeOutcome:
        for node in self.nodes:
            if node.key == key:
                return node

        msg = f"dag {self.dag!r} v{self.version}: no outcome for node {key!r}"
        raise DagRunError(msg)

    def failure(self, key: str) -> FailureResult:
        """Сбой узла key: его собственная ошибка, если группу сорвал он сам,
        иначе срыв группы глазами узла; узел прошёл — DagRunError."""
        node = self.node(key)
        if not isinstance(node.artifact, FailureResult):
            msg = (
                f"dag {self.dag!r} v{self.version}: node {key!r} succeeded: "
                f"{node.content}"
            )
            raise DagRunError(msg)

        if not isinstance(node.artifact, GroupFailureResult):
            return node.artifact

        if node.artifact.own:
            return node.artifact.cause

        return node.artifact

    def _failed(self) -> Iterator[NodeOutcome]:
        for node in self.nodes:
            if node.failed():
                yield node


class DagHandle:
    """Запущенный DAG: итог каждого узла по готовности и общий итог.

    Отдаёт его DagRunner.start; чат ждёт по ключу узла (tool_call_id) ответ
    для модели, запуск без модели ждёт outcome целиком.
    """

    def __init__(
        self, dag: DagSpec, tasks: Mapping[str, asyncio.Task[NodeOutcome]]
    ) -> None:
        self._dag = dag
        self._tasks = dict(tasks)

    def has(self, key: str) -> bool:
        """Узел с таким ключом есть в DAG."""
        return key in self._tasks

    async def result(self, key: str) -> NodeOutcome:
        """Итог узла key; ждёт его конца."""
        task = self._tasks.get(key)
        if task is None:
            msg = f"dag {self._dag.name!r} v{self._dag.version}: no node {key!r}"
            raise DagRunError(msg)

        return await task

    async def outcome(self) -> DagOutcome:
        """Итог всего DAG: ждёт конца каждого узла."""
        nodes: list[NodeOutcome] = []
        for node in self._dag.nodes:
            nodes.append(await self._tasks[node.key])

        return DagOutcome(dag=self._dag.name, version=self._dag.version, nodes=nodes)

    def done(self) -> bool:
        """Все узлы закончились."""
        return not any(self._pending())

    def _pending(self) -> Iterator[asyncio.Task[NodeOutcome]]:
        for task in self._tasks.values():
            if not task.done():
                yield task

    def cancel(self) -> None:
        for task in self._tasks.values():
            task.cancel()


class DagRunner:
    """Запуск DAG: план, группа и по задаче на каждый узел.

    Инструменты приходят по именам — обёрнутые инструменты реестра (чат:
    набор хода, планировщик: for_headless). Узел зовётся как вызов модели
    (ToolCall с id = ключ узла): обвязки получают tool_call_id, лента —
    события вызова. Контекст вызова (CallContext, callbacks) задачи
    наследуют от вызывающего.
    """

    def __init__(
        self, tools: Mapping[str, BaseTool], timings: StreamTimings, pipe_bytes: int
    ) -> None:
        """pipe_bytes — буфер пайпов каналов узла, который его не назвал."""
        self._tools = dict(tools)
        self._timings = timings
        self._planner = DagPlanner(ToolStreamSpecs.of, pipe_bytes)
        self._failures = FailurePacker()

    async def run(
        self, dag: DagSpec, config: RunnableConfig | None = None
    ) -> DagOutcome:
        handle = self.start(dag, config)

        return await handle.outcome()

    def start(self, dag: DagSpec, config: RunnableConfig | None = None) -> DagHandle:
        """План и группа по описанию; узлы стартуют задачами сразу.

        config — конфиг langchain вызова (callbacks ленты), с ним зовётся
        каждый инструмент; без него — конфиг контекста.
        """
        plan = self._planner.plan(dag)

        for node in dag.nodes:
            self._tool_of(node)

        group = StreamGroupRun(plan, self._timings)
        logger.info("dag %s v%d started: %s", dag.name, dag.version, group.labels())

        tasks: dict[str, asyncio.Task[NodeOutcome]] = {}
        for node in dag.nodes:
            tasks[node.key] = asyncio.create_task(
                self._run_node(group, node, config),
                name=f"dag {dag.name} v{dag.version}: {node.key}",
            )

        return DagHandle(dag, tasks)

    def _tool_of(self, node: DagNode) -> BaseTool:
        tool = self._tools.get(node.tool)
        if tool is None:
            msg = (
                f"dag node {node.label()}: tool {node.tool!r} is not available; "
                f"available tools: {sorted(self._tools)}"
            )
            raise DagRunError(msg)

        return tool

    async def _run_node(
        self, group: StreamGroupRun, node: DagNode, config: RunnableConfig | None
    ) -> NodeOutcome:
        """Вызов узла под ручкой группы; сбой до открытия срывает группу."""
        tool = self._tool_of(node)
        call = ToolCall(
            name=node.tool, args=dict(node.args), id=node.key, type="tool_call"
        )

        token = PipelineSlot.set(group.slot(node.key))
        try:
            message = await tool.ainvoke(call, config)
        except Exception as exc:
            failure = self._failures.pack(exc)
            group.refuse(node.key, failure)
            return self._failed(node, failure)
        finally:
            PipelineSlot.reset(token)

        outcome = self._outcome_of(node, message)
        if isinstance(outcome.artifact, FailureResult):
            # узел, закончившийся через обёртку, группа уже знает — повтор пуст
            group.refuse(node.key, outcome.artifact)

        return outcome

    @staticmethod
    def _failed(node: DagNode, failure: FailureResult) -> NodeOutcome:
        content, artifact = failure.packed()

        return NodeOutcome(
            key=node.key, tool=node.tool, content=content, artifact=artifact
        )

    @staticmethod
    def _outcome_of(node: DagNode, message: object) -> NodeOutcome:
        if not isinstance(message, ToolMessage):
            msg = (
                f"dag node {node.label()}: expected a ToolMessage from the tool, "
                f"got {type(message).__name__}"
            )
            raise DagRunError(msg)

        artifact = message.artifact
        if not isinstance(artifact, ToolResultBase):
            msg = (
                f"dag node {node.label()}: expected a ToolResultBase artifact, "
                f"got {type(artifact).__name__}"
            )
            raise DagRunError(msg)

        return NodeOutcome(
            key=node.key, tool=node.tool, content=message.text, artifact=artifact
        )
