"""Исполнитель DAG вызовов инструментов.

DagRunner принимает описание DAG (boba.toolkit.dag) и обёрнутые инструменты
реестра и сам запускает каждый узел — другого места исполнения инструментов
нет. План делит узлы на группы исполнения: узлы, связанные каналами, — одна
группа, узел инструмента без портов — группа из него одного. Каждая группа
идёт по правилу «все или никто» (boba.toolkit.chain), каждый узел — под
ручкой своей группы в CallAmbient; сбой группы другие группы не трогает.
Вызов узла идёт через обвязки инструмента: права, журнал, отмена, упаковка
ошибок. Узел, не дошедший до запуска (права, аргументы), срывает свою
группу сразу. Итог — DagOutcome: результат каждого узла. Один исполнитель
служит чату (узлы — вызовы ответа модели и подготовки хода) и запуску без
модели.

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
from typing import Literal

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict

from boba.toolkit.chain import (
    CallAmbient,
    GroupFailureResult,
    StreamGroupRun,
    StreamTimings,
)
from boba.toolkit.dag import DagNode, DagPlanner, DagSpec
from boba.toolkit.failure import FailurePacker
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import (
    FailureResult,
    ToolArtifact,
    ToolResult,
)

__all__ = ["DagHandle", "DagOutcome", "DagRunError", "DagRunner", "NodeOutcome"]

logger = logging.getLogger(__name__)


class DagRunError(RuntimeError):
    """Узел DAG нельзя исполнить: инструмента нет либо его ответ не результат."""


class NodeOutcome(BaseModel):
    """Итог узла: текст для модели и результат семейства; сбой — FailureResult.

    errored — вызов кончился ошибкой самого вызова: инструмент поднял
    исключение до тела (аргументы, права) либо ответил сообщением со
    статусом error. Сообщение для модели несёт тот же статус.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    tool: str
    content: str
    artifact: ToolResult
    errored: bool = False

    def failed(self) -> bool:
        return isinstance(self.artifact, FailureResult)

    def ok(self) -> bool:
        """Вызов дошёл до тела, и тело вернуло удачный результат."""
        if self.errored:
            return False

        return self.artifact.ok

    def error_text(self) -> str:
        """Текст отказа для журнала; пустой — вызов удался."""
        if self.errored:
            return self.content

        if not self.artifact.ok:
            return self.artifact.llm_view()

        return ""

    def message(self, tool_call_id: str) -> ToolMessage:
        """Итог узла сообщением инструмента для модели."""
        status: Literal["success", "error"] = "success"
        if self.errored:
            status = "error"

        return ToolMessage(
            content=self.content,
            artifact=self.artifact,
            name=self.tool,
            tool_call_id=tool_call_id,
            status=status,
        )


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
    """Запущенный DAG: общий итог и остановка.

    Отдаёт его DagRunner.start; вызывающий ждёт outcome либо гасит узлы.
    """

    def __init__(
        self, dag: DagSpec, tasks: Mapping[str, asyncio.Task[NodeOutcome]]
    ) -> None:
        self._dag = dag
        self._tasks = dict(tasks)

    async def outcome(self) -> DagOutcome:
        """Итог всего DAG: ждёт конца каждого узла."""
        nodes: list[NodeOutcome] = []
        for node in self._dag.nodes:
            nodes.append(await self._tasks[node.key])

        return DagOutcome(dag=self._dag.name, version=self._dag.version, nodes=nodes)

    def cancel(self) -> None:
        for task in self._tasks.values():
            task.cancel()


class DagRunner:
    """Запуск DAG: план, группы и по задаче на каждый узел.

    Инструменты приходят по именам — обёрнутые инструменты реестра (чат:
    набор хода, планировщик: for_headless). Узел зовётся как вызов модели
    (ToolCall с id = ключ узла) под ручкой своей группы: обвязки получают
    tool_call_id, лента — события вызова. Контекст вызова (CallContext,
    callbacks) задачи наследуют от вызывающего.
    """

    def __init__(
        self,
        tools: Mapping[str, BaseTool],
        specs: StreamSpecs,
        ambient: CallAmbient,
        timings: StreamTimings,
        pipe_bytes: int,
    ) -> None:
        """specs — потоковые декларации инструментов; ambient — обстановка
        вызова, в которую ставится ручка узла; pipe_bytes — буфер
        пайпов каналов узла, который его не назвал."""
        self._tools = dict(tools)
        self._ambient = ambient
        self._timings = timings
        self._planner = DagPlanner(specs.of, pipe_bytes)
        self._failures = FailurePacker()

    async def run(self, dag: DagSpec) -> DagOutcome:
        """Исполнить DAG и дождаться всех узлов; обрыв ожидания гасит узлы."""
        handle = self.start(dag)

        try:
            return await handle.outcome()
        except BaseException:
            handle.cancel()
            raise

    def start(self, dag: DagSpec) -> DagHandle:
        """План по описанию; узлы стартуют задачами сразу. Инструменты
        зовутся с конфигом langchain из контекста вызывающего."""
        for node in dag.nodes:
            self._tool_of(node)

        plans = self._planner.plan(dag)

        tasks: dict[str, asyncio.Task[NodeOutcome]] = {}
        for planned in plans:
            group = StreamGroupRun(planned, self._timings)
            logger.info(
                "dag %s v%d group started: %s", dag.name, dag.version, group.labels()
            )

            for member in planned.nodes():
                node = dag.node(member.key)
                tasks[node.key] = asyncio.create_task(
                    self._run_node(group, node),
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

    async def _run_node(self, group: StreamGroupRun, node: DagNode) -> NodeOutcome:
        """Вызов узла под ручкой группы; его итог группа узнаёт всегда.

        Сбой до открытия вызова срывает группу. Узел, открытый обёрткой
        запуска, группа уже знает — повторное сообщение пусто; узел, который
        обёртку запуска не проходит, группа узнаёт только отсюда.
        """
        with self._ambient.in_slot(group.slot(node.key)):
            outcome = await self._invoke(node)

        cause: FailureResult | None = None
        if isinstance(outcome.artifact, FailureResult):
            cause = outcome.artifact

        group.done(node.key, cause=cause, stopped=False)

        return outcome

    async def _invoke(self, node: DagNode) -> NodeOutcome:
        """Вызов инструмента узла; исключение вызова — итог-ошибка узла."""
        tool = self._tool_of(node)
        call = ToolCall(
            name=node.tool, args=dict(node.args), id=node.key, type="tool_call"
        )

        try:
            message = await tool.ainvoke(call)
        except Exception as exc:
            return self._failed(node, self._failures.pack(exc))

        return self._outcome_of(node, message)

    @staticmethod
    def _failed(node: DagNode, failure: FailureResult) -> NodeOutcome:
        content, artifact = failure.packed()

        return NodeOutcome(
            key=node.key,
            tool=node.tool,
            content=content,
            artifact=artifact,
            errored=True,
        )

    @staticmethod
    def _outcome_of(node: DagNode, message: object) -> NodeOutcome:
        if not isinstance(message, ToolMessage):
            msg = (
                f"dag node {node.label()}: expected a ToolMessage from the tool, "
                f"got {type(message).__name__}"
            )
            raise DagRunError(msg)

        artifact = ToolArtifact.revive(message.artifact)
        if artifact is None:
            msg = (
                f"dag node {node.label()}: expected a ToolResultBase artifact, "
                f"got {type(message.artifact).__name__}"
            )
            raise DagRunError(msg)

        return NodeOutcome(
            key=node.key,
            tool=node.tool,
            content=message.text,
            artifact=artifact,
            errored=message.status == "error",
        )
