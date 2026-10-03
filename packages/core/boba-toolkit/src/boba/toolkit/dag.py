"""Описание DAG вызовов инструментов и его перевод в план исполнения.

DagSpec — сериализуемая модель графа: узлы — вызовы инструментов в том
виде, в каком их делает модель (имя и аргументы), каналы названы в
аргументах полями с именами портов инструмента. Описание живёт отдельно от
исполнения: его даёт ответ модели, файл или хранилище, а исполнитель
принимает только его. DagPlanner переводит описание в планы групп
исполнения (StreamPlan) по декларациям портов инструментов: узлы, связанные
каналами, собираются в одну группу и исполняются по правилу «все или никто»,
узел без каналов — группа из него одного. Все проверки графа каналов
остаются в StreamPlan. WorkflowResult — итог вызова связки для клиента:
результаты узлов одним конвертом.

Ошибки:
StreamPlanError — поле порта узла не имя канала, pipe_bytes не число либо
    граф нарушен (правила StreamPlan).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from boba.toolkit.chain import (
    StreamInput,
    StreamNode,
    StreamOutput,
    StreamPlan,
    StreamPlanError,
)
from boba.toolkit.ports import PortDecl, StreamSpec
from boba.toolkit.result import ChatView, Fact, FactsBlock, ToolResult, ToolResultBase

__all__ = [
    "DagNode",
    "DagPlanner",
    "DagSpec",
    "WorkflowNodeResult",
    "WorkflowResult",
]


class DagNode(BaseModel):
    """Узел DAG: вызов инструмента — его имя и аргументы вызова.

    Поля каналов (имена портов инструмента) и pipe_bytes лежат среди
    аргументов, как в вызове модели; ключ узла в DAG уникален, из чата им
    служит tool_call_id. title — имя узла, каким его назвал автор описания
    (ключ узла вызова workflow): им узел называется в текстах отказов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: Mapping[str, JsonValue] = {}
    title: str = ""

    def label(self) -> str:
        if self.title:
            return f"{self.tool} ({self.title})"

        return f"{self.tool} ({self.key})"


class DagSpec(BaseModel):
    """Описание DAG: имя, версия и узлы; ключи узлов уникальны."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    nodes: Sequence[DagNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_are_unique(self) -> DagSpec:
        repeated = sorted(set(self._repeated_keys()))
        if repeated:
            msg = (
                f"dag {self.name!r} v{self.version}: node keys must be unique, "
                f"repeated: {repeated}"
            )
            raise ValueError(msg)

        return self

    def _repeated_keys(self) -> Iterator[str]:
        seen: set[str] = set()
        for node in self.nodes:
            if node.key in seen:
                yield node.key

            seen.add(node.key)

    def node(self, key: str) -> DagNode:
        for node in self.nodes:
            if node.key == key:
                return node

        msg = f"dag {self.name!r} v{self.version}: no node with key {key!r}"
        raise KeyError(msg)


@dataclass
class _Component:
    """Набор узлов, уже связанных каналами, и имена этих каналов."""

    nodes: list[StreamNode]
    channels: set[str]


class ChannelComponents:
    """Связные компоненты узлов по общим именам каналов.

    DagPlanner отдаёт сюда узлы в порядке описания; узел, называющий канал
    уже собранной компоненты, присоединяется к ней, а называющий каналы
    нескольких — сливает их в одну. Узел без общих каналов остаётся
    компонентой из себя одного.
    """

    def __init__(self) -> None:
        self._components: list[_Component] = []
        self._order: dict[str, int] = {}

    def add(self, node: StreamNode) -> None:
        self._order[node.key] = len(self._order)
        channels = set(self._channels_of(node))

        joined = _Component(nodes=[node], channels=set(channels))
        apart: list[_Component] = []
        for component in self._components:
            if component.channels.isdisjoint(channels):
                apart.append(component)
                continue

            joined.nodes.extend(component.nodes)
            joined.channels.update(component.channels)

        apart.append(joined)
        self._components = apart

    def components(self) -> Iterator[_Component]:
        """Компоненты в порядке их первых узлов, узлы — в порядке описания."""
        ordered: list[_Component] = []
        for component in self._components:
            nodes = sorted(component.nodes, key=self._position)
            ordered.append(_Component(nodes=nodes, channels=component.channels))

        ordered.sort(key=self._first_position)

        yield from ordered

    def _position(self, node: StreamNode) -> int:
        return self._order[node.key]

    def _first_position(self, component: _Component) -> int:
        return self._order[component.nodes[0].key]

    @staticmethod
    def _channels_of(node: StreamNode) -> Iterator[str]:
        for bound in node.inputs:
            yield bound.channel

        for output in node.outputs:
            yield output.channel


class DagPlanner:
    """Перевод описания DAG в план исполнения по декларациям инструментов.

    Декларации приходят функцией «имя инструмента → StreamSpec» (в
    приложении — StreamSpecs.of). Для каждого узла планировщик читает
    из аргументов поля портов: у одиночного порта — имя канала строкой, у
    порта-списка — список имён; каждый порт назван обязательно. У писателя
    читается pipe_bytes; узел его не назвал — берётся pipe_bytes
    планировщика (дефолт секции [stream_groups]). Узлы делятся на группы по
    общим каналам: имена каналов и связывают вызовы в группу, узел
    инструмента без портов остаётся группой из себя одного. Правила графа
    каждой группы проверяет StreamPlan.
    """

    PIPE_FIELD: ClassVar[str] = "pipe_bytes"

    def __init__(self, specs: Callable[[str], StreamSpec], pipe_bytes: int) -> None:
        self._specs = specs
        self._default_pipe_bytes = pipe_bytes

    def plan(self, dag: DagSpec) -> tuple[StreamPlan, ...]:
        """Планы групп исполнения в порядке первых узлов описания."""
        nodes: list[StreamNode] = []
        for node in dag.nodes:
            nodes.append(self._node(node, self._specs(node.tool)))

        # опечатка в канале разносит писателя и читателя по группам
        StreamPlan(nodes)

        components = ChannelComponents()
        for planned in nodes:
            components.add(planned)

        plans: list[StreamPlan] = []
        for component in components.components():
            plans.append(StreamPlan(component.nodes))

        return tuple(plans)

    def _node(self, node: DagNode, spec: StreamSpec) -> StreamNode:
        outputs: list[StreamOutput] = []
        for port, channel in self._channels(node, spec.outbound()):
            outputs.append(StreamOutput(port=port, channel=channel))

        inputs: list[StreamInput] = []
        for port, channel in self._channels(node, spec.inbound()):
            inputs.append(StreamInput(port=port, channel=channel))

        pipe_bytes = 0
        if outputs:
            pipe_bytes = self._pipe_bytes(node)

        return StreamNode(
            key=node.key,
            tool=node.tool,
            spec=spec,
            outputs=tuple(outputs),
            inputs=tuple(inputs),
            pipe_bytes=pipe_bytes,
            title=node.title,
        )

    def _channels(
        self, node: DagNode, ports: Sequence[PortDecl]
    ) -> Iterator[tuple[str, str]]:
        """Пары (порт, канал) одной стороны узла из его аргументов."""
        for port in ports:
            value = node.args.get(port.name)
            if value is None:
                msg = (
                    f"stream plan: {node.label()} names no channel for port "
                    f"{port.name!r}; every port of a node is bound explicitly"
                )
                raise StreamPlanError(msg)

            for channel in self._names(node, port, value):
                yield port.name, channel

    def _names(self, node: DagNode, port: PortDecl, value: JsonValue) -> list[str]:
        if not port.many:
            return [self._name(node, port, value)]

        if not isinstance(value, list):
            msg = (
                f"stream plan: {node.label()} field {port.name!r} expects a list "
                f"of channel names, got {value!r}"
            )
            raise StreamPlanError(msg)

        names: list[str] = []
        for item in value:
            names.append(self._name(node, port, item))

        return names

    @staticmethod
    def _name(node: DagNode, port: PortDecl, value: JsonValue) -> str:
        if not isinstance(value, str):
            msg = (
                f"stream plan: {node.label()} field {port.name!r} expects a "
                f"channel name, got {value!r}"
            )
            raise StreamPlanError(msg)

        if not value:
            msg = (
                f"stream plan: {node.label()} field {port.name!r} names an "
                "empty channel"
            )
            raise StreamPlanError(msg)

        return value

    def _pipe_bytes(self, node: DagNode) -> int:
        value = node.args.get(self.PIPE_FIELD, self._default_pipe_bytes)

        if isinstance(value, bool):
            raise StreamPlanError(self._pipe_text(node, value))

        if not isinstance(value, int):
            raise StreamPlanError(self._pipe_text(node, value))

        if value < 0:
            raise StreamPlanError(self._pipe_text(node, value))

        return value

    def _pipe_text(self, node: DagNode, value: JsonValue) -> str:
        return (
            f"stream plan: {node.label()} field {self.PIPE_FIELD!r} expects a "
            f"non-negative integer, got {value!r}"
        )


class WorkflowNodeResult(BaseModel):
    """Итог узла workflow: какой инструмент с чем вызван и чем он кончился.

    key — имя узла, данное моделью; call_id — идентификатор вызова узла, под
    которым идут его журнал и шаг ленты. errored — вызов кончился ошибкой
    самого вызова (аргументы, права), а не результатом инструмента.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    call_id: str
    tool: str
    args: Mapping[str, Any]
    errored: bool
    content: str
    result: ToolResult


class WorkflowResult(ToolResultBase):
    """Итог вызова workflow: результаты его узлов в порядке описания.

    Модель описывает связку инструментов одним вызовом workflow; исполнитель
    DAG отдаёт итог каждого узла, а этот результат несёт их модели и
    истории одним конвертом. Клиент оживляет его по kind и раскрывает в
    шаги узлов.
    """

    kind: Literal["workflow"] = "workflow"
    nodes: Sequence[WorkflowNodeResult]

    def llm_view(self) -> str:
        parts: list[str] = []
        for node in self.nodes:
            parts.append(f"[{node.key}] {node.tool}:\n{node.content}")

        return "\n\n".join(parts)

    def chat_view(self) -> ChatView:
        return ChatView(markdown=FactsBlock(facts=self._facts()).markdown())

    def _facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for node in self.nodes:
            status = "ok"
            if not node.result.ok:
                status = "failed"

            facts.append(Fact(key=f"{node.key} ({node.tool})", value=status))

        return facts
