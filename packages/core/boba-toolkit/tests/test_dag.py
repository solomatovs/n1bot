"""Описание DAG: разбор из toml/json и перевод в план каналов."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ValidationError

from boba.toolkit.chain import StreamInput, StreamOutput, StreamPlan, StreamPlanError
from boba.toolkit.dag import DagNode, DagPlan, DagPlanner, DagSpec
from boba.toolkit.ports import Inbound, Outbound, StreamSpec


class ChunkHead(BaseModel):
    kind: Literal["chunk"] = "chunk"


class _Source(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    out: Annotated[Outbound[ChunkHead], None]


class _Transform(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    feed: Annotated[Inbound[ChunkHead], None]
    extra: Annotated[Sequence[Inbound[ChunkHead]], None]
    out: Annotated[Outbound[ChunkHead], None]


class _Shard(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    feed: Annotated[Inbound[ChunkHead], None]
    outs: Annotated[Sequence[Outbound[ChunkHead]], None]


class _Sink(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    feed: Annotated[Inbound[ChunkHead], None]


SPECS = {
    "source": StreamSpec.of_schema(_Source),
    "transform": StreamSpec.of_schema(_Transform),
    "shard": StreamSpec.of_schema(_Shard),
    "sink": StreamSpec.of_schema(_Sink),
}

TOML = """
name = "orders"
version = 2

[[nodes]]
key = "src"
tool = "source"

[nodes.args]
sql = "select 1"
out = "raw"
pipe_bytes = 65536

[[nodes]]
key = "t"
tool = "transform"

[nodes.args]
feed = "raw"
extra = ["side"]
out = "cooked"
pipe_bytes = 0

[[nodes]]
key = "side"
tool = "source"

[nodes.args]
sql = "select 2"
out = "side"

[[nodes]]
key = "sink"
tool = "sink"

[nodes.args]
table = "orders"
feed = "cooked"
"""


def _spec(name: str) -> StreamSpec:
    spec = SPECS.get(name)
    if spec is None:
        return StreamSpec()

    return spec


def _node(key: str, tool: str, **args: object) -> DagNode:
    return DagNode.model_validate({"key": key, "tool": tool, "args": args})


def _dag(*nodes: DagNode) -> DagSpec:
    return DagSpec(name="t", version=1, nodes=nodes)


PLANNER = DagPlanner(_spec, 0)


def _one_group(dag: DagSpec) -> StreamPlan:
    """План единственной группы DAG: узлы описания связаны каналами."""
    plan = PLANNER.plan(dag)
    assert len(plan.groups) == 1

    return plan.groups[0].plan


class TestDagSpec:
    def test_toml_is_the_model_call_as_a_file(self) -> None:
        """Узел файла — тот же вызов, что делает модель: инструмент и
        аргументы с полями портов; план из него — тот же граф."""
        dag = DagSpec.model_validate(tomllib.loads(TOML))

        plan = _one_group(dag)

        assert dag.node("sink").args == {"table": "orders", "feed": "cooked"}
        assert plan.inputs_of("t") == (
            StreamInput(port="feed", channel="raw"),
            StreamInput(port="extra", channel="side"),
        )
        assert plan.outputs_of("src") == (StreamOutput(port="out", channel="raw"),)
        assert plan.pipe_bytes_of("raw") == 65536
        assert plan.pipe_bytes_of("side") == 0
        assert plan.route("cooked").readers[0].node == "sink"

    def test_json_round_trip_keeps_the_dag(self) -> None:
        dag = DagSpec.model_validate(tomllib.loads(TOML))

        restored = DagSpec.model_validate_json(dag.model_dump_json())

        assert restored == dag

    def test_repeated_keys_are_refused(self) -> None:
        with pytest.raises(ValidationError, match=r"repeated: \['a'\]"):
            _dag(_node("a", "source", out="x"), _node("a", "sink", feed="x"))

    def test_unknown_node_key_is_refused(self) -> None:
        dag = _dag(_node("a", "source", out="x"), _node("b", "sink", feed="x"))

        with pytest.raises(KeyError, match="no node with key 'zz'"):
            dag.node("zz")


class TestDagPlanner:
    def test_list_port_takes_every_named_channel(self) -> None:
        plan = _one_group(
            _dag(
                _node("src", "source", out="raw"),
                _node("sh", "shard", feed="raw", outs=["a", "b"]),
                _node("la", "sink", feed="a"),
                _node("lb", "sink", feed="b"),
            )
        )

        assert plan.outputs_of("sh") == (
            StreamOutput(port="outs", channel="a"),
            StreamOutput(port="outs", channel="b"),
        )

    def test_single_port_refuses_a_list(self) -> None:
        with pytest.raises(StreamPlanError, match=r"sink \(s\) field 'feed' expects"):
            PLANNER.plan(
                _dag(_node("a", "source", out="x"), _node("s", "sink", feed=["x"]))
            )

    def test_list_port_refuses_a_string(self) -> None:
        with pytest.raises(StreamPlanError, match="'outs' expects a list"):
            PLANNER.plan(
                _dag(
                    _node("src", "source", out="raw"),
                    _node("sh", "shard", feed="raw", outs="a"),
                    _node("la", "sink", feed="a"),
                )
            )

    def test_empty_channel_name_is_refused(self) -> None:
        with pytest.raises(StreamPlanError, match="names an empty channel"):
            PLANNER.plan(
                _dag(_node("a", "source", out=""), _node("s", "sink", feed="x"))
            )

    def test_every_port_must_be_named(self) -> None:
        with pytest.raises(StreamPlanError, match="names no channel for port 'extra'"):
            PLANNER.plan(
                _dag(
                    _node("src", "source", out="raw"),
                    _node("t", "transform", feed="raw", out="cooked"),
                    _node("s", "sink", feed="cooked"),
                )
            )

    def test_pipe_bytes_must_be_a_non_negative_integer(self) -> None:
        for bad in ("big", True, -1):
            with pytest.raises(StreamPlanError, match="'pipe_bytes' expects"):
                PLANNER.plan(
                    _dag(
                        _node("a", "source", out="x", pipe_bytes=bad),
                        _node("s", "sink", feed="x"),
                    )
                )

    def test_pipe_bytes_of_a_reader_is_ignored(self) -> None:
        plan = _one_group(
            _dag(
                _node("a", "source", out="x"),
                _node("s", "sink", feed="x", pipe_bytes="whatever"),
            )
        )

        assert plan.pipe_bytes_of("x") == 0

    def test_graph_rules_come_from_the_plan(self) -> None:
        with pytest.raises(StreamPlanError, match="has no readers"):
            PLANNER.plan(_dag(_node("a", "source", out="x")))

    def test_tool_without_ports_is_a_group_of_one(self) -> None:
        plan = PLANNER.plan(_dag(_node("p", "plain", query="select 1")))

        assert len(plan.groups) == 1
        assert plan.groups[0].name == "p"
        assert plan.groups[0].plan.inputs_of("p") == ()
        assert plan.groups[0].plan.outputs_of("p") == ()


class TestDagGroups:
    """Имена каналов связывают узлы в группы; узел без каналов — группа из
    него одного с именем по ключу узла."""

    @staticmethod
    def _layout(plan: DagPlan) -> dict[str, list[str]]:
        """Имя группы → ключи её узлов, в порядке плана."""
        layout: dict[str, list[str]] = {}
        for group in plan.groups:
            keys: list[str] = []
            for node in group.plan.nodes():
                keys.append(node.key)

            layout[group.name] = keys

        return layout

    def test_unrelated_pipelines_are_separate_groups(self) -> None:
        plan = PLANNER.plan(
            _dag(
                _node("a", "source", out="x"),
                _node("c", "source", out="y"),
                _node("b", "sink", feed="x"),
                _node("d", "sink", feed="y"),
            )
        )

        assert self._layout(plan) == {"x": ["a", "b"], "y": ["c", "d"]}

    def test_a_node_reading_two_groups_merges_them(self) -> None:
        plan = PLANNER.plan(
            _dag(
                _node("a", "source", out="raw"),
                _node("side", "source", out="side"),
                _node("t", "transform", feed="raw", extra=["side"], out="cooked"),
                _node("s", "sink", feed="cooked"),
            )
        )

        assert self._layout(plan) == {"cooked+raw+side": ["a", "side", "t", "s"]}

    def test_calls_without_ports_get_groups_of_their_own(self) -> None:
        plan = PLANNER.plan(
            _dag(
                _node("p1", "plain", query="select 1"),
                _node("a", "source", out="x"),
                _node("p2", "plain", query="select 2"),
                _node("b", "sink", feed="x"),
            )
        )

        assert self._layout(plan) == {"p1": ["p1"], "x": ["a", "b"], "p2": ["p2"]}
        assert list(self._layout(plan)) == ["p1", "x", "p2"]

    def test_a_broken_group_refuses_the_whole_plan(self) -> None:
        with pytest.raises(StreamPlanError, match="has no readers"):
            PLANNER.plan(
                _dag(
                    _node("p", "plain", query="select 1"),
                    _node("a", "source", out="x"),
                )
            )
