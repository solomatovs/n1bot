"""Спека workflow: задачи, порты, рёбра, разбор и рендер YAML.

Ошибки:
WorkflowSpecError — спека не разобрана или нарушает правила; несёт замечания.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar

import yaml
from jinja2 import StrictUndefined, meta
from jinja2.exceptions import TemplateError, TemplateSyntaxError
from jinja2.sandbox import ImmutableSandboxedEnvironment
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

__all__ = [
    "ArgTemplate",
    "Edge",
    "EdgeKind",
    "EdgeText",
    "Ident",
    "IssueCode",
    "PortDirection",
    "PortKind",
    "PortRef",
    "SpecIssue",
    "TaskSpec",
    "WorkflowSpec",
    "WorkflowSpecError",
]


Ident = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=64),
]
"""Имя задачи, порта, аргумента: идентификатор без точек и пробелов."""

WorkflowName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_-]*$", max_length=64),
]


class IssueCode(StrEnum):
    """Код замечания к спеке; страница подсвечивает по нему узел или ребро."""

    YAML = "yaml"
    SCHEMA = "schema"
    PORT_SYNTAX = "port_syntax"
    EDGE_SYNTAX = "edge_syntax"
    EDGE_KIND = "edge_kind"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_DENIED = "tool_denied"
    TOOL_CHAT_ONLY = "tool_chat_only"
    UNKNOWN_ARG = "unknown_arg"
    MISSING_ARG = "missing_arg"
    PORTS_NOT_ALLOWED = "ports_not_allowed"
    UNKNOWN_TASK = "unknown_task"
    UNKNOWN_PORT = "unknown_port"
    PORT_DIRECTION = "port_direction"
    SELF_EDGE = "self_edge"
    DUPLICATE_EDGE = "duplicate_edge"
    ARG_BOUND_TWICE = "arg_bound_twice"
    ARG_BOUND_AND_SET = "arg_bound_and_set"
    TEMPLATE_SYNTAX = "template_syntax"
    TEMPLATE_UNBOUND = "template_unbound"
    PORT_UNCONNECTED = "port_unconnected"
    STAGE_DEADLOCK = "stage_deadlock"
    CYCLE = "cycle"


class SpecIssue(BaseModel):
    """Одно замечание: код, к чему относится (задача, ребро, пусто — вся спека)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: IssueCode
    where: str = ""
    message: str

    def render(self) -> str:
        if not self.where:
            return f"{self.code}: {self.message}"

        return f"{self.code} at {self.where}: {self.message}"


class WorkflowSpecError(Exception):
    """Спека негодна; замечания собраны все сразу, а не по одному."""

    def __init__(self, issues: Sequence[SpecIssue]) -> None:
        self.issues: tuple[SpecIssue, ...] = tuple(issues)
        super().__init__(self._text())

    def _text(self) -> str:
        lines: list[str] = []
        for issue in self.issues:
            lines.append(issue.render())

        return "; ".join(lines)


class PortDirection(StrEnum):
    """Направление fd-порта относительно задачи."""

    READ = "read"
    WRITE = "write"


class PortKind(StrEnum):
    """Что стоит за портом в ребре."""

    TASK = "task"
    """Голое имя задачи: control-ребро."""
    RESULT = "result"
    ARG = "arg"
    FD = "fd"


class PortToken(StrEnum):
    """Лексемы записи порта `task.port`."""

    SEP = "."
    RESULT = "result"
    ARGS = "args"


class PortRef(BaseModel):
    """Порт задачи: `task`, `task.result`, `task.args.<имя>`, `task.<fd-порт>`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: Ident
    kind: PortKind
    name: str = ""
    """Имя аргумента или fd-порта; у task и result пусто."""

    def render(self) -> str:
        if self.kind is PortKind.TASK:
            return self.task

        if self.kind is PortKind.RESULT:
            return f"{self.task}{PortToken.SEP}{PortToken.RESULT}"

        if self.kind is PortKind.ARG:
            return (
                f"{self.task}{PortToken.SEP}{PortToken.ARGS}{PortToken.SEP}{self.name}"
            )

        return f"{self.task}{PortToken.SEP}{self.name}"

    @classmethod
    def parse(cls, raw: str) -> PortRef:
        """Разбор записи порта; негодная запись — WorkflowSpecError(PORT_SYNTAX)."""
        task, _, rest = raw.strip().partition(PortToken.SEP)

        try:
            return cls._of(task, rest)
        except ValidationError as exc:
            issue = SpecIssue(
                code=IssueCode.PORT_SYNTAX,
                where=raw,
                message=(
                    f"port reference {raw!r} is not valid: {exc.errors()[0]['msg']}"
                ),
            )
            raise WorkflowSpecError([issue]) from exc

    @classmethod
    def _of(cls, task: str, rest: str) -> PortRef:
        """Часть после имени задачи: пусто, result, args.<имя> или <fd-порт>."""
        if not rest:
            return cls(task=task, kind=PortKind.TASK)

        if rest == PortToken.RESULT:
            return cls(task=task, kind=PortKind.RESULT)

        head, _, name = rest.partition(PortToken.SEP)
        if not name:
            return cls(task=task, kind=PortKind.FD, name=head)

        if head == PortToken.ARGS and PortToken.SEP not in name:
            return cls(task=task, kind=PortKind.ARG, name=name)

        issue = SpecIssue(
            code=IssueCode.PORT_SYNTAX,
            where=f"{task}{PortToken.SEP}{rest}",
            message=(
                f"expected task, task.result, task.args.<name> or task.<port>, "
                f"got {task}{PortToken.SEP}{rest}"
            ),
        )
        raise WorkflowSpecError([issue])


class EdgeKind(StrEnum):
    """Вид ребра по портам; от вида зависит порядок выполнения."""

    STREAM = "stream"
    """fd → fd: задачи выполняются одновременно."""
    VALUE = "value"
    """result → args.<имя>: приёмник после источника."""
    CONTROL = "control"
    """task → task: приёмник после источника."""

    @classmethod
    def of(cls, src: PortRef, dst: PortRef) -> EdgeKind | None:
        """Вид по паре портов; None — такая пара ребром не бывает."""
        if src.kind is PortKind.FD and dst.kind is PortKind.FD:
            return cls.STREAM

        if src.kind is PortKind.RESULT and dst.kind is PortKind.ARG:
            return cls.VALUE

        if src.kind is PortKind.TASK and dst.kind is PortKind.TASK:
            return cls.CONTROL

        return None

    @property
    def orders(self) -> bool:
        """Ребро задаёт «после», а не «одновременно»."""
        return self is not EdgeKind.STREAM


class Edge(BaseModel):
    """Ребро между портами с уже определённым видом."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    src: PortRef
    dst: PortRef
    kind: EdgeKind

    def render(self) -> str:
        return f"{self.src.render()} {EdgeText.ARROW} {self.dst.render()}"

    @classmethod
    def of(cls, src: PortRef, dst: PortRef) -> Edge:
        """Ребро по паре портов; несочетаемые порты — WorkflowSpecError(EDGE_KIND)."""
        kind = EdgeKind.of(src, dst)
        if kind is None:
            issue = SpecIssue(
                code=IssueCode.EDGE_KIND,
                where=f"{src.render()} {EdgeText.ARROW} {dst.render()}",
                message=(
                    f"{src.kind} port cannot feed {dst.kind} port: "
                    "expected fd -> fd, result -> args.<name> or task -> task"
                ),
            )
            raise WorkflowSpecError([issue])

        return cls(src=src, dst=dst, kind=kind)


class EdgeText:
    """Запись ребра строкой: `a.out -> b.src`, `[b, c] -> d`, `a -> [b, c]`."""

    ARROW: ClassVar[str] = "->"
    LIST_OPEN: ClassVar[str] = "["
    LIST_CLOSE: ClassVar[str] = "]"
    LIST_SEP: ClassVar[str] = ","

    @classmethod
    def parse(cls, raw: str) -> tuple[Edge, ...]:
        """Рёбра из строки; список с любой стороны раскрывается в произведение."""
        left, arrow, right = raw.partition(cls.ARROW)
        if not arrow or cls.ARROW in right:
            issue = SpecIssue(
                code=IssueCode.EDGE_SYNTAX,
                where=raw,
                message=(
                    f"edge must contain exactly one '{cls.ARROW}' between "
                    f"its sides, got {raw!r}"
                ),
            )
            raise WorkflowSpecError([issue])

        sources = cls._side(left, raw)
        targets = cls._side(right, raw)

        edges: list[Edge] = []
        for src, dst in itertools.product(sources, targets):
            edges.append(Edge.of(src, dst))

        return tuple(edges)

    @classmethod
    def _side(cls, text: str, raw: str) -> tuple[PortRef, ...]:
        stripped = text.strip()
        if not stripped:
            issue = SpecIssue(
                code=IssueCode.EDGE_SYNTAX,
                where=raw,
                message=(
                    f"edge side is empty, expected a port or a [list] on both "
                    f"sides of '{cls.ARROW}'"
                ),
            )
            raise WorkflowSpecError([issue])

        listed = stripped.startswith(cls.LIST_OPEN)
        if listed and not stripped.endswith(cls.LIST_CLOSE):
            issue = SpecIssue(
                code=IssueCode.EDGE_SYNTAX,
                where=raw,
                message=(
                    f"port list {stripped!r} opens with '{cls.LIST_OPEN}' "
                    f"but does not close with '{cls.LIST_CLOSE}'"
                ),
            )
            raise WorkflowSpecError([issue])

        if not listed:
            return (PortRef.parse(stripped),)

        inner = stripped[len(cls.LIST_OPEN) : -len(cls.LIST_CLOSE)]

        refs: list[PortRef] = []
        for item in inner.split(cls.LIST_SEP):
            refs.append(PortRef.parse(item))

        return tuple(refs)


class TaskSpec(BaseModel):
    """Задача: инструмент, аргументы, fd-порты (у инструментов с портами в задаче)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: Ident
    args: Mapping[str, Any] = Field(default_factory=dict)
    ports: Mapping[Ident, PortDirection] = Field(default_factory=dict)


class ArgTemplate:
    """Аргумент-шаблон: `{{ задача }}` — результат задачи-источника.

    Шаблоном считается только аргумент, в который ведёт ребро-значение.
    Движок — Jinja2 в неизменяемой песочнице, без автоэкранирования
    (аргументы — SQL и команды, не HTML); неизвестное имя — ошибка.
    """

    ENV: ClassVar[ImmutableSandboxedEnvironment] = ImmutableSandboxedEnvironment(
        undefined=StrictUndefined, autoescape=False
    )

    @classmethod
    def names_of(cls, text: str) -> frozenset[str]:
        """Имена, на которые ссылается шаблон; негодный синтаксис — TEMPLATE_SYNTAX."""
        try:
            tree = cls.ENV.parse(text)
        except TemplateSyntaxError as exc:
            issue = SpecIssue(
                code=IssueCode.TEMPLATE_SYNTAX,
                where=text,
                message=f"argument template is not valid Jinja2: {exc.message}",
            )
            raise WorkflowSpecError([issue]) from exc

        return frozenset(meta.find_undeclared_variables(tree))

    @classmethod
    def render(cls, text: str, values: Mapping[str, str]) -> str:
        try:
            return cls.ENV.from_string(text).render(**values)
        except TemplateError as exc:
            given = sorted(values)
            issue = SpecIssue(
                code=IssueCode.TEMPLATE_SYNTAX,
                where=text,
                message=(
                    f"argument template rendering failed with values for {given}: {exc}"
                ),
            )
            raise WorkflowSpecError([issue]) from exc


class SpecField(StrEnum):
    """Ключи YAML-спеки."""

    NAME = "name"
    DESCRIPTION = "description"
    TASKS = "tasks"
    EDGES = "edges"


class _Draft(BaseModel):
    """Спека как пришла: рёбра ещё строками."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: WorkflowName
    description: str = ""
    tasks: Mapping[Ident, TaskSpec] = Field(default_factory=dict)
    edges: Sequence[str] = ()


class WorkflowSpec(BaseModel):
    """Разобранная спека: задачи и типизированные рёбра."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: WorkflowName
    description: str = ""
    tasks: Mapping[Ident, TaskSpec]
    edges: tuple[Edge, ...] = ()

    @classmethod
    def parse(cls, raw: object) -> WorkflowSpec:
        """Спека из данных YAML/JSON; все замечания разбора — одной ошибкой."""
        try:
            draft = _Draft.model_validate(raw)
        except ValidationError as exc:
            issues = list(cls._schema_issues(exc))
            raise WorkflowSpecError(issues) from exc

        issues: list[SpecIssue] = []
        edges: list[Edge] = []
        for line in draft.edges:
            try:
                edges.extend(EdgeText.parse(line))
            except WorkflowSpecError as exc:
                issues.extend(exc.issues)

        if issues:
            raise WorkflowSpecError(issues)

        return cls(
            name=draft.name,
            description=draft.description,
            tasks=draft.tasks,
            edges=tuple(edges),
        )

    @classmethod
    def parse_yaml(cls, text: str) -> WorkflowSpec:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            issue = SpecIssue(
                code=IssueCode.YAML,
                message=f"workflow spec yaml is not parsed: {exc}",
            )
            raise WorkflowSpecError([issue]) from exc

        return cls.parse(raw)

    def render(self) -> dict[str, Any]:
        """Данные для YAML: рёбра строками, как их пишет автор."""
        tasks: dict[str, Any] = {}
        for name, task in self.tasks.items():
            tasks[name] = task.model_dump(mode="json", exclude_defaults=True)

        edges: list[str] = []
        for edge in self.edges:
            edges.append(edge.render())

        data: dict[str, Any] = {SpecField.NAME.value: self.name}
        if self.description:
            data[SpecField.DESCRIPTION.value] = self.description
        data[SpecField.TASKS.value] = tasks
        if edges:
            data[SpecField.EDGES.value] = edges

        return data

    def render_yaml(self) -> str:
        return yaml.safe_dump(self.render(), sort_keys=False, allow_unicode=True)

    @staticmethod
    def _schema_issues(exc: ValidationError) -> Iterator[SpecIssue]:
        for error in exc.errors():
            parts: list[str] = []
            for part in error["loc"]:
                parts.append(str(part))

            yield SpecIssue(
                code=IssueCode.SCHEMA,
                where=PortToken.SEP.join(parts),
                message=error["msg"],
            )
