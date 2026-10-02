"""Описание DAG потоковых вызовов и его перевод в план каналов.

DagSpec — сериализуемая модель графа: узлы — вызовы инструментов в том
виде, в каком их делает модель (имя и аргументы), каналы названы в
аргументах полями с именами портов инструмента. Описание живёт отдельно от
исполнения: его даёт ответ модели, файл или хранилище, а исполнитель
принимает только его. DagPlanner переводит описание в StreamPlan по
декларациям портов инструментов; все проверки графа остаются в StreamPlan.

Ошибки:
StreamPlanError — поле порта узла не имя канала, pipe_bytes не число либо
    граф нарушен (правила StreamPlan).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from boba.toolkit.chain import (
    StreamInput,
    StreamNode,
    StreamOutput,
    StreamPlan,
    StreamPlanError,
)
from boba.toolkit.ports import PortDecl, StreamSpec

__all__ = ["DagNode", "DagPlanner", "DagSpec"]


class DagNode(BaseModel):
    """Узел DAG: вызов инструмента — его имя и аргументы вызова.

    Поля каналов (имена портов инструмента) и pipe_bytes лежат среди
    аргументов, как в вызове модели; ключ узла в DAG уникален, из чата им
    служит tool_call_id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: Mapping[str, JsonValue] = {}

    def label(self) -> str:
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


class DagPlanner:
    """Перевод описания DAG в план каналов по декларациям инструментов.

    Декларации приходят функцией «имя инструмента → StreamSpec» (в
    приложении — ToolStreamSpecs.of). Для каждого узла планировщик читает
    из аргументов поля портов: у одиночного порта — имя канала строкой, у
    порта-списка — список имён; каждый порт назван обязательно. У писателя
    читается pipe_bytes; узел его не назвал — берётся pipe_bytes
    планировщика (дефолт секции [stream_groups]). Правила графа проверяет
    StreamPlan.
    """

    PIPE_FIELD: ClassVar[str] = "pipe_bytes"

    def __init__(self, specs: Callable[[str], StreamSpec], pipe_bytes: int) -> None:
        self._specs = specs
        self._default_pipe_bytes = pipe_bytes

    def plan(self, dag: DagSpec) -> StreamPlan:
        return StreamPlan(list(self._nodes(dag)))

    def _nodes(self, dag: DagSpec) -> Iterator[StreamNode]:
        for node in dag.nodes:
            yield self._node(node)

    def _node(self, node: DagNode) -> StreamNode:
        spec = self._specs(node.tool)

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
