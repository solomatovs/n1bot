"""Исполнитель DAG вызовов инструментов.

DagRunner принимает описание DAG (boba.toolkit.dag) и обёрнутые инструменты
хоста и сам запускает каждый узел — другого места исполнения инструментов
нет. План делит узлы на группы исполнения: узлы, связанные каналами, — одна
группа, узел инструмента без портов — группа из него одного. Каждая группа
идёт по правилу «все или никто» (boba.toolkit.chain), каждый узел — под
ручкой своей группы в CallAmbient; сбой группы другие группы не трогает.
Вызов узла идёт к телу путём NodeCalls и через обвязки инструмента: права,
журнал, отмена, упаковка ошибок. Узел, не дошедший до запуска (права,
аргументы), срывает свою группу сразу. Итог — DagOutcome: результат каждого
узла. Один исполнитель служит чату (узлы — вызовы ответа модели и
подготовки хода) и запуску без модели.

Ошибки:
StreamPlanError — описание не переводится в план: поле порта, pipe_bytes,
    правила графа.
DagRunError — узел зовёт инструмент, которого нет среди переданных.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from boba.toolkit.chain import (
    CallAmbient,
    GroupFailureResult,
    StreamGroupRun,
    StreamTimings,
)
from boba.toolkit.dag import (
    DagNode,
    DagPlanner,
    DagSpec,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
)
from boba.toolkit.failure import FailurePacker
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import FailureResult
from boba.toolrun.hosted import HostedTool

__all__ = ["DagHandle", "DagOutcome", "DagRunError", "DagRunner"]

logger = logging.getLogger(__name__)


class DagRunError(RuntimeError):
    """Узел DAG нельзя исполнить: инструмента нет среди переданных."""


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
    набор хода, планировщик: for_headless) — вместе со своими карточками
    (ToolSchema.cards_of): карточку узла получает путь вызова. Узел зовётся
    под ручкой своей
    группы путём calls (NodeCalls): сервис зовёт тело напрямую, чат проводит
    вызов через свой компонент, и лента получает события вызова. Обвязки
    получают идентификатор вызова — ключ узла. Контекст вызова задачи
    наследуют от вызывающего.
    """

    def __init__(  # noqa: PLR0913 — исполнитель собирается всеми входами запуска
        self,
        tools: Mapping[str, HostedTool],
        cards: Mapping[str, ToolCard],
        specs: StreamSpecs,
        ambient: CallAmbient,
        timings: StreamTimings,
        pipe_bytes: int,
        calls: NodeCalls,
    ) -> None:
        """cards — карточки инструментов tools по тем же именам; specs —
        потоковые декларации инструментов; ambient — обстановка
        вызова, в которую ставится ручка узла; pipe_bytes — буфер
        пайпов каналов узла, который его не назвал; calls — путь вызова
        узла к телу."""
        self._tools = dict(tools)
        self._calls = calls
        self._cards = dict(cards)
        self._ambient = ambient
        self._timings = timings
        self._planner = DagPlanner(specs.of, pipe_bytes)
        self._failures = FailurePacker()
        self._outcomes = NodeOutcomes()

    async def run(self, dag: DagSpec) -> DagOutcome:
        """Исполнить DAG и дождаться всех узлов; обрыв ожидания гасит узлы."""
        handle = self.start(dag)

        try:
            return await handle.outcome()
        except BaseException:
            handle.cancel()
            raise

    def start(self, dag: DagSpec) -> DagHandle:
        """План по описанию; узлы стартуют задачами сразу в контексте
        вызывающего."""
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

    def _tool_of(self, node: DagNode) -> HostedTool:
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
        """Вызов узла путём NodeCalls: тело узла — вызов его инструмента."""
        card = self._cards[node.tool]

        return await self._calls.conducted(card, node, self._called)

    async def _called(self, node: DagNode) -> NodeOutcome:
        """Тело узла: вызов инструмента; исключение вызова — итог-отказ узла."""
        tool = self._tool_of(node)

        try:
            return await tool.call(node)
        except Exception as exc:
            return self._outcomes.refused(node, self._failures.pack(exc))
