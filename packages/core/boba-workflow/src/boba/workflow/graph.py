"""Граф workflow: каталог инструментов, проверка спеки, стадии, автомат запуска.

Ошибки:
WorkflowSpecError — спека нарушает правила графа; замечания собраны все сразу.
WorkflowPlanError — нарушен протокол автомата: задача не в том состоянии.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.access import ToolAvailability
from boba.toolkit.calls import ArgView, TextArg
from boba.toolkit.result import ToolResult
from boba.workflow.spec import (
    ArgTemplate,
    Edge,
    EdgeKind,
    IssueCode,
    PortDirection,
    PortKind,
    PortRef,
    SpecIssue,
    TaskSpec,
    WorkflowSpec,
    WorkflowSpecError,
)

__all__ = [
    "RunState",
    "RunStatus",
    "Stage",
    "TaskState",
    "TaskStatus",
    "ToolArg",
    "ToolCatalog",
    "ToolFacts",
    "ToolPort",
    "WorkflowGraph",
    "WorkflowPlan",
    "WorkflowPlanError",
]


class ToolArg(BaseModel):
    """Аргумент инструмента: обязательность, вид для страницы, описание."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    required: bool = False
    view: ArgView = TextArg()
    description: str = ""


class ToolPort(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    direction: PortDirection


class ToolFacts(BaseModel):
    """Что домен знает об инструменте: доступность, аргументы, fd-порты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    availability: ToolAvailability
    description: str = ""
    args: tuple[ToolArg, ...] = ()
    ports: tuple[ToolPort, ...] = ()
    results: tuple[str, ...] = ()
    """Виды ToolResult, которые инструмент объявил через Produces."""
    task_ports: bool = False
    """Порты объявляются в задаче, а не в сигнатуре (bash)."""

    def arg(self, name: str) -> ToolArg | None:
        for arg in self.args:
            if arg.name == name:
                return arg

        return None

    def port(self, name: str) -> ToolPort | None:
        for port in self.ports:
            if port.name == name:
                return port

        return None

    def required_args(self) -> Iterator[str]:
        for arg in self.args:
            if not arg.required:
                continue

            yield arg.name


ToolCatalog = Mapping[str, ToolFacts]
"""Инструменты по имени: сервис собирает его из реестра под субъекта."""


class Stage(BaseModel):
    """Стадия: задачи, связанные потоками, стартуют вместе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ID_PREFIX: ClassVar[str] = "stage:"

    id: str
    tasks: tuple[str, ...]
    streams: tuple[Edge, ...] = ()
    after: tuple[str, ...] = ()
    """Стадии, чьего завершения ждёт эта."""

    @classmethod
    def id_of(cls, tasks: Sequence[str]) -> str:
        return f"{cls.ID_PREFIX}{min(tasks)}"

    def readers_of(self, src: PortRef) -> tuple[PortRef, ...]:
        readers: list[PortRef] = []
        for edge in self.streams:
            if edge.src != src:
                continue

            readers.append(edge.dst)

        return tuple(readers)

    def writers_of(self, dst: PortRef) -> tuple[PortRef, ...]:
        writers: list[PortRef] = []
        for edge in self.streams:
            if edge.dst != dst:
                continue

            writers.append(edge.src)

        return tuple(writers)

    def writer_ports(self) -> tuple[PortRef, ...]:
        """Пишущие порты стадии без повторов, в порядке первого появления."""
        seen: list[PortRef] = []
        for edge in self.streams:
            if edge.src in seen:
                continue

            seen.append(edge.src)

        return tuple(seen)

    def reader_ports(self) -> tuple[PortRef, ...]:
        seen: list[PortRef] = []
        for edge in self.streams:
            if edge.dst in seen:
                continue

            seen.append(edge.dst)

        return tuple(seen)


class _Components:
    """Union-find по именам задач."""

    def __init__(self, names: Sequence[str]) -> None:
        self._parent: dict[str, str] = {}
        for name in names:
            self._parent[name] = name

    def find(self, name: str) -> str:
        root = name
        while self._parent[root] != root:
            root = self._parent[root]

        while self._parent[name] != root:
            self._parent[name], name = root, self._parent[name]

        return root

    def union(self, left: str, right: str) -> None:
        self._parent[self.find(left)] = self.find(right)

    def groups(self) -> tuple[tuple[str, ...], ...]:
        """Компоненты в детерминированном порядке: по наименьшему имени."""
        members: dict[str, list[str]] = {}
        for name in self._parent:
            members.setdefault(self.find(name), []).append(name)

        groups: list[tuple[str, ...]] = []
        for names in members.values():
            groups.append(tuple(sorted(names)))

        groups.sort()
        return tuple(groups)


class _Checker:
    """Проверка спеки против каталога и сборка стадий."""

    def __init__(self, spec: WorkflowSpec, catalog: ToolCatalog) -> None:
        self._spec = spec
        self._catalog = catalog
        self._issues: list[SpecIssue] = []
        self._bound_args: dict[str, dict[str, set[str]]] = {}
        """task → аргумент → задачи-источники, привязанные рёбрами."""
        self._templates: dict[PortRef, frozenset[str]] = {}
        self._used_ports: set[PortRef] = set()

    def run(self) -> tuple[tuple[Stage, ...], Mapping[str, tuple[ArgBinding, ...]]]:
        self._tasks()
        self._edges()
        self._ports_connected()
        self._missing_args()
        self._templates_bound()
        self._raise_if_any()

        stages = self._stages()
        ordered = self._order(stages)
        self._raise_if_any()

        return ordered, self._bindings()

    def _bindings(self) -> Mapping[str, tuple[ArgBinding, ...]]:
        """Привязки рёбер-значений к аргументам, готовые к подстановке на запуске."""
        bindings: dict[str, tuple[ArgBinding, ...]] = {}
        for task_name, args in self._bound_args.items():
            items = list(self._task_bindings(task_name, args))
            if items:
                bindings[task_name] = tuple(items)

        return bindings

    def _task_bindings(
        self, task_name: str, args: Mapping[str, set[str]]
    ) -> Iterator[ArgBinding]:
        task = self._spec.tasks[task_name]
        for arg, sources in args.items():
            if not sources:
                continue

            template = ""
            if arg in task.args:
                template = str(task.args[arg])

            yield ArgBinding(arg=arg, sources=tuple(sorted(sources)), template=template)

    def _raise_if_any(self) -> None:
        if not self._issues:
            return

        raise WorkflowSpecError(self._issues)

    def _issue(self, code: IssueCode, where: str, message: str) -> None:
        self._issues.append(SpecIssue(code=code, where=where, message=message))

    def _facts(self, task: str) -> ToolFacts | None:
        """Факты инструмента задачи; None — задача или инструмент уже отмечены."""
        spec = self._spec.tasks.get(task)
        if spec is None:
            return None

        return self._catalog.get(spec.tool)

    def _tasks(self) -> None:
        for name, task in self._spec.tasks.items():
            self._task(name, task)

    def _task(self, name: str, task: TaskSpec) -> None:
        facts = self._catalog.get(task.tool)
        if facts is None:
            self._issue(IssueCode.UNKNOWN_TOOL, name, f"unknown tool: {task.tool}")
            return

        if facts.availability is ToolAvailability.DENIED:
            self._issue(
                IssueCode.TOOL_DENIED, name, f"tool is not allowed: {task.tool}"
            )
            return

        if facts.availability is ToolAvailability.CHAT_ONLY:
            self._issue(
                IssueCode.TOOL_CHAT_ONLY,
                name,
                f"tool works only in the chat: {task.tool}",
            )
            return

        for arg in task.args:
            if facts.arg(arg) is not None:
                continue

            self._issue(IssueCode.UNKNOWN_ARG, name, f"unknown argument: {arg}")

        if task.ports and not facts.task_ports:
            self._issue(
                IssueCode.PORTS_NOT_ALLOWED,
                name,
                f"ports of {task.tool} come from its signature, not from the task",
            )

    def _edges(self) -> None:
        seen: list[Edge] = []
        for edge in self._spec.edges:
            if edge in seen:
                self._issue(IssueCode.DUPLICATE_EDGE, edge.render(), "edge repeats")
                continue

            seen.append(edge)
            self._edge(edge)

    def _edge(self, edge: Edge) -> None:
        where = edge.render()
        if edge.src.task == edge.dst.task:
            self._issue(IssueCode.SELF_EDGE, where, "task cannot feed itself")
            return

        known = self._known_task(edge.src.task, where)
        known = self._known_task(edge.dst.task, where) and known
        if not known:
            return

        self._port(edge.src, PortDirection.WRITE, where)
        self._port(edge.dst, PortDirection.READ, where)

        if edge.kind is EdgeKind.VALUE:
            self._bind_arg(edge, where)

        if edge.kind is EdgeKind.STREAM:
            self._used_ports.add(edge.src)
            self._used_ports.add(edge.dst)

    def _known_task(self, task: str, where: str) -> bool:
        if task in self._spec.tasks:
            return True

        self._issue(IssueCode.UNKNOWN_TASK, where, f"unknown task: {task}")
        return False

    def _port(self, ref: PortRef, expected: PortDirection, where: str) -> None:
        facts = self._facts(ref.task)
        if facts is None:
            return

        if ref.kind is PortKind.ARG:
            if facts.arg(ref.name) is None:
                self._issue(
                    IssueCode.UNKNOWN_ARG, where, f"unknown argument: {ref.render()}"
                )
            return

        if ref.kind is not PortKind.FD:
            return

        direction = self._fd_direction(ref, facts)
        if direction is None:
            self._issue(IssueCode.UNKNOWN_PORT, where, f"unknown port: {ref.render()}")
            return

        if direction is expected:
            return

        self._issue(
            IssueCode.PORT_DIRECTION,
            where,
            f"port {ref.render()} is {direction}, edge needs {expected}",
        )

    def _fd_direction(self, ref: PortRef, facts: ToolFacts) -> PortDirection | None:
        if facts.task_ports:
            return self._spec.tasks[ref.task].ports.get(ref.name)

        port = facts.port(ref.name)
        if port is None:
            return None

        return port.direction

    def _bind_arg(self, edge: Edge, where: str) -> None:
        """Заданный аргумент — шаблон с именем источника; незаданный — одно ребро."""
        dst = edge.dst
        sources = self._bound_args.setdefault(dst.task, {}).setdefault(dst.name, set())

        task = self._spec.tasks[dst.task]
        if dst.name not in task.args:
            if sources:
                self._issue(
                    IssueCode.ARG_BOUND_TWICE,
                    where,
                    f"argument bound twice: {dst.render()}",
                )
                return

            sources.add(edge.src.task)
            return

        names = self._template_names(dst, task.args[dst.name])
        if names is None:
            return

        if edge.src.task not in names:
            self._issue(
                IssueCode.ARG_BOUND_AND_SET,
                where,
                f"argument is set but does not mention {{{{ {edge.src.task} }}}}: "
                f"{dst.render()}",
            )
            return

        sources.add(edge.src.task)

    def _template_names(self, dst: PortRef, value: object) -> frozenset[str] | None:
        """Имена в шаблоне аргумента; None — синтаксис негоден, замечание записано."""
        cached = self._templates.get(dst)
        if cached is not None:
            return cached

        if not isinstance(value, str):
            self._templates[dst] = frozenset()
            return frozenset()

        try:
            names = ArgTemplate.names_of(value)
        except WorkflowSpecError as exc:
            for issue in exc.issues:
                self._issues.append(issue.model_copy(update={"where": dst.render()}))
            return None

        self._templates[dst] = names
        return names

    def _templates_bound(self) -> None:
        """Каждое имя шаблона должно приходить ребром-значением."""
        for dst, names in self._templates.items():
            sources = self._bound_args.get(dst.task, {}).get(dst.name, set())
            unbound = sorted(names - sources)
            if not unbound:
                continue

            self._issue(
                IssueCode.TEMPLATE_UNBOUND,
                dst.render(),
                f"template mentions tasks without an edge: {', '.join(unbound)}",
            )

    def _ports_connected(self) -> None:
        for name, task in self._spec.tasks.items():
            for port in self._declared_ports(name, task):
                ref = PortRef(task=name, kind=PortKind.FD, name=port)
                if ref in self._used_ports:
                    continue

                self._issue(
                    IssueCode.PORT_UNCONNECTED,
                    name,
                    f"port has no edge: {ref.render()}",
                )

    def _declared_ports(self, name: str, task: TaskSpec) -> Iterator[str]:
        facts = self._facts(name)
        if facts is None:
            return

        if facts.availability is not ToolAvailability.AVAILABLE:
            return

        if facts.task_ports:
            yield from task.ports
            return

        for port in facts.ports:
            yield port.name

    def _missing_args(self) -> None:
        for name, task in self._spec.tasks.items():
            facts = self._facts(name)
            if facts is None:
                continue

            bound = self._bound_args.get(name, {})
            for arg in facts.required_args():
                if arg in task.args:
                    continue

                if arg in bound:
                    continue

                self._issue(IssueCode.MISSING_ARG, name, f"required argument: {arg}")

    def _stages(self) -> tuple[Stage, ...]:
        components = _Components(list(self._spec.tasks))
        for edge in self._spec.edges:
            if edge.kind is not EdgeKind.STREAM:
                continue

            components.union(edge.src.task, edge.dst.task)

        stages: list[Stage] = []
        for tasks in components.groups():
            streams = self._streams_within(tasks)
            stages.append(Stage(id=Stage.id_of(tasks), tasks=tasks, streams=streams))

        return tuple(stages)

    def _streams_within(self, tasks: Sequence[str]) -> tuple[Edge, ...]:
        streams: list[Edge] = []
        for edge in self._spec.edges:
            if edge.kind is not EdgeKind.STREAM:
                continue

            if edge.src.task not in tasks:
                continue

            streams.append(edge)

        return tuple(streams)

    def _order(self, stages: Sequence[Stage]) -> tuple[Stage, ...]:
        self._task_cycle()

        after = self._stage_deps(stages)

        sorter: TopologicalSorter[str] = TopologicalSorter()
        for stage in stages:
            sorter.add(stage.id, *sorted(after[stage.id]))

        try:
            order = list(sorter.static_order())
        except CycleError as exc:
            self._issue(IssueCode.CYCLE, "", f"stages form a cycle: {exc.args[1]}")
            return ()

        by_id: dict[str, Stage] = {}
        for stage in stages:
            by_id[stage.id] = stage

        ordered: list[Stage] = []
        for stage_id in order:
            stage = by_id[stage_id]
            ordered.append(
                stage.model_copy(update={"after": tuple(sorted(after[stage_id]))})
            )

        return tuple(ordered)

    def _stage_deps(self, stages: Sequence[Stage]) -> dict[str, set[str]]:
        """Зависимости «после» между стадиями; ребро внутри стадии — дедлок."""
        stage_of: dict[str, Stage] = {}
        for stage in stages:
            for task in stage.tasks:
                stage_of[task] = stage

        after: dict[str, set[str]] = {}
        for stage in stages:
            after[stage.id] = set()

        for edge in self._spec.edges:
            if not edge.kind.orders:
                continue

            src = stage_of[edge.src.task]
            dst = stage_of[edge.dst.task]
            if src.id == dst.id:
                self._issue(
                    IssueCode.STAGE_DEADLOCK,
                    edge.render(),
                    "stream-joined tasks run together, one cannot wait for another",
                )
                continue

            after[dst.id].add(src.id)

        return after

    def _task_cycle(self) -> None:
        """Цикл по любым рёбрам — в том числе потоковый внутри стадии."""
        sorter: TopologicalSorter[str] = TopologicalSorter()
        for name in self._spec.tasks:
            sorter.add(name)

        for edge in self._spec.edges:
            sorter.add(edge.dst.task, edge.src.task)

        try:
            sorter.prepare()
        except CycleError as exc:
            self._issue(IssueCode.CYCLE, "", f"tasks form a cycle: {exc.args[1]}")


class ArgBinding(BaseModel):
    """Аргумент задачи, который заполняют рёбра-значения.

    template — заданное в спеке значение-шаблон с именами источников; пустой
    template — аргумент не задан, и единственный источник подставляется целиком.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arg: str
    sources: tuple[str, ...] = Field(min_length=1)
    template: str = ""

    def value(self, texts: Mapping[str, str]) -> str:
        """Значение аргумента по текстам результатов источников."""
        if not self.template:
            return texts[self.sources[0]]

        values: dict[str, str] = {}
        for source in self.sources:
            values[source] = texts[source]

        return ArgTemplate.render(self.template, values)


class WorkflowGraph(BaseModel):
    """Проверенная спека: стадии в порядке запуска и привязки аргументов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: WorkflowSpec
    stages: tuple[Stage, ...]
    bindings: Mapping[str, tuple[ArgBinding, ...]] = Field(default_factory=dict)

    @classmethod
    def build(cls, spec: WorkflowSpec, catalog: ToolCatalog) -> WorkflowGraph:
        stages, bindings = _Checker(spec, catalog).run()
        return cls(spec=spec, stages=stages, bindings=bindings)

    def bindings_of(self, task: str) -> tuple[ArgBinding, ...]:
        bound = self.bindings.get(task)
        if bound is None:
            return ()

        return bound

    def sources_of(self, task: str) -> frozenset[str]:
        """Задачи, чьи результаты нужны аргументам task."""
        sources: set[str] = set()
        for binding in self.bindings_of(task):
            sources.update(binding.sources)

        return frozenset(sources)

    def args_of(self, task: str, texts: Mapping[str, str]) -> dict[str, Any]:
        """Аргументы вызова: свои из спеки плюс подстановки по привязкам."""
        args: dict[str, Any] = dict(self.spec.tasks[task].args)
        for binding in self.bindings_of(task):
            args[binding.arg] = binding.value(texts)

        return args

    def stage_of(self, task: str) -> Stage:
        for stage in self.stages:
            if task in stage.tasks:
                return stage

        raise KeyError(task)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"

    @property
    def terminal(self) -> bool:
        return self not in (TaskStatus.PENDING, TaskStatus.RUNNING)

    @property
    def reportable(self) -> bool:
        """Исход, который сообщает раннер; skipped ставит только автомат."""
        return self in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.STOPPED)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def terminal(self) -> bool:
        return self not in (RunStatus.PENDING, RunStatus.RUNNING)


class TaskState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: TaskStatus = TaskStatus.PENDING
    call_id: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    result: ToolResult | None = None
    """Итог инструмента по завершении; страница показывает его по kind."""

    @property
    def elapsed_ms(self) -> int:
        if self.started_at is None:
            return 0

        if self.finished_at is None:
            return 0

        return int((self.finished_at - self.started_at).total_seconds() * 1000)


class RunState(BaseModel):
    """Снимок запуска: граф, статус и состояние задач."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: WorkflowGraph
    status: RunStatus
    tasks: Mapping[str, TaskState]

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.DONE


class WorkflowPlanError(Exception):
    """Раннер нарушил протокол автомата."""


class WorkflowPlan:
    """Автомат запуска: какие стадии готовы, что с задачами, снимок.

    Раннер зовёт ready() в цикле, стартует задачи готовых стадий и сообщает
    started/finished; отказ зависимости помечает стадию skipped сам автомат.
    """

    def __init__(self, graph: WorkflowGraph) -> None:
        self._graph = graph

        self._tasks: dict[str, TaskState] = {}
        for name in graph.spec.tasks:
            self._tasks[name] = TaskState()

        self._stages: dict[str, Stage] = {}
        for stage in graph.stages:
            self._stages[stage.id] = stage

        self._sorter: TopologicalSorter[str] = TopologicalSorter()
        for stage in graph.stages:
            self._sorter.add(stage.id, *stage.after)
        self._sorter.prepare()

        self._launched: set[str] = set()
        self._settled: set[str] = set()
        self._failed: set[str] = set()
        self._stopped = False

    @property
    def done(self) -> bool:
        return all(state.status.terminal for state in self._tasks.values())

    def ready(self) -> tuple[Stage, ...]:
        """Стадии к запуску; заблокированные отказом или стопом гасятся тут же."""
        launch: list[Stage] = []

        pending = list(self._sorter.get_ready())
        while pending:
            stage = self._stages[pending.pop()]

            if self._stopped:
                self._skip(stage, TaskStatus.STOPPED)
                pending.extend(self._sorter.get_ready())
                continue

            if self._blocked(stage):
                self._skip(stage, TaskStatus.SKIPPED)
                pending.extend(self._sorter.get_ready())
                continue

            self._launched.add(stage.id)
            launch.append(stage)

        return tuple(launch)

    def started(self, task: str, call_id: str, at: datetime) -> None:
        state = self._state(task)
        if state.status is not TaskStatus.PENDING:
            raise WorkflowPlanError(f"task {task} started twice: {state.status}")

        self._tasks[task] = state.model_copy(
            update={"status": TaskStatus.RUNNING, "call_id": call_id, "started_at": at}
        )

    def finished(
        self,
        task: str,
        status: TaskStatus,
        at: datetime,
        error: str = "",
        result: ToolResult | None = None,
    ) -> None:
        state = self._state(task)
        if state.status is not TaskStatus.RUNNING:
            raise WorkflowPlanError(f"task {task} finished while {state.status}")

        if not status.reportable:
            raise WorkflowPlanError(f"task {task}: {status} is not a reported outcome")

        self._tasks[task] = state.model_copy(
            update={
                "status": status,
                "finished_at": at,
                "error": error,
                "result": result,
            }
        )
        self._settle_if_complete(self._graph.stage_of(task))

    def stop(self) -> None:
        """Стоп: незапущенное гасится, работающее ждёт finished(STOPPED) от раннера."""
        self._stopped = True

        for stage_id in self._launched:
            stage = self._stages[stage_id]
            for task in stage.tasks:
                state = self._tasks[task]
                if state.status is not TaskStatus.PENDING:
                    continue

                self._tasks[task] = state.model_copy(
                    update={"status": TaskStatus.STOPPED}
                )

            self._settle_if_complete(stage)

    def snapshot(self) -> RunState:
        return RunState(
            graph=self._graph,
            status=self._status(),
            tasks=dict(self._tasks),
        )

    def _state(self, task: str) -> TaskState:
        state = self._tasks.get(task)
        if state is None:
            raise WorkflowPlanError(f"unknown task: {task}")

        return state

    def _blocked(self, stage: Stage) -> bool:
        return any(dep in self._failed for dep in stage.after)

    def _skip(self, stage: Stage, status: TaskStatus) -> None:
        for task in stage.tasks:
            self._tasks[task] = self._tasks[task].model_copy(update={"status": status})

        self._settle(stage)

    def _settle_if_complete(self, stage: Stage) -> None:
        if stage.id in self._settled:
            return

        for task in stage.tasks:
            if not self._tasks[task].status.terminal:
                return

        self._settle(stage)

    def _settle(self, stage: Stage) -> None:
        self._settled.add(stage.id)
        self._sorter.done(stage.id)

        for task in stage.tasks:
            if self._tasks[task].status is TaskStatus.DONE:
                continue

            self._failed.add(stage.id)
            return

    def _status(self) -> RunStatus:
        if not self.done:
            return self._live_status()

        return self._final_status()

    def _live_status(self) -> RunStatus:
        if self._launched:
            return RunStatus.RUNNING

        return RunStatus.PENDING

    def _final_status(self) -> RunStatus:
        statuses: list[TaskStatus] = []
        for state in self._tasks.values():
            statuses.append(state.status)

        if TaskStatus.STOPPED in statuses:
            return RunStatus.STOPPED

        if TaskStatus.FAILED in statuses:
            return RunStatus.FAILED

        if TaskStatus.SKIPPED in statuses:
            return RunStatus.FAILED

        return RunStatus.DONE
