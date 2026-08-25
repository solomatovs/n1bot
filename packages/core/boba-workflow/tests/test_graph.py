"""Граф: стадии, порядок, все правила проверки спеки против каталога."""

from __future__ import annotations

from typing import Any

import pytest
from catalog import catalog
from test_spec import EXAMPLE

from boba.workflow import (
    IssueCode,
    PortKind,
    PortRef,
    WorkflowGraph,
    WorkflowSpec,
    WorkflowSpecError,
)


def build(raw: dict[str, Any]) -> WorkflowGraph:
    return WorkflowGraph.build(WorkflowSpec.parse(raw), catalog())


def issues_of(raw: dict[str, Any]) -> list[IssueCode]:
    with pytest.raises(WorkflowSpecError) as caught:
        build(raw)

    return [issue.code for issue in caught.value.issues]


def bash(command: str, **ports: str) -> dict[str, Any]:
    return {"tool": "bash", "args": {"command": command}, "ports": ports}


def test_example_stages_and_order() -> None:
    graph = WorkflowGraph.build(WorkflowSpec.parse_yaml(EXAMPLE), catalog())

    by_tasks = {stage.tasks: stage for stage in graph.stages}
    assert set(by_tasks) == {("count", "dump", "load"), ("ids",), ("check",)}

    stream = by_tasks[("count", "dump", "load")]
    assert stream.after == ()
    assert len(stream.streams) == 2

    check = by_tasks[("check",)]
    assert set(check.after) == {stream.id, by_tasks[("ids",)].id}
    assert graph.stages[-1] is check

    assert graph.stage_of("dump") is stream
    (binding,) = graph.bindings_of("check")
    assert binding.sources == ("ids",)
    assert graph.sources_of("check") == frozenset({"ids"})


def test_stream_fan_out_and_fan_in() -> None:
    graph = build(
        {
            "name": "w",
            "tasks": {
                "a": bash("a > $out", out="write"),
                "b": bash("b > $out", out="write"),
                "c": bash("c < $src", src="read"),
                "d": bash("d < $src", src="read"),
            },
            "edges": ["a.out -> [c.src, d.src]", "b.out -> c.src"],
        }
    )

    stage = graph.stages[0]
    a_out = PortRef(task="a", kind=PortKind.FD, name="out")
    c_src = PortRef(task="c", kind=PortKind.FD, name="src")
    assert [ref.task for ref in stage.readers_of(a_out)] == ["c", "d"]
    assert [ref.task for ref in stage.writers_of(c_src)] == ["a", "b"]
    assert [ref.task for ref in stage.writer_ports()] == ["a", "b"]
    assert [ref.task for ref in stage.reader_ports()] == ["c", "d"]


def test_two_placeholders_in_one_argument() -> None:
    graph = build(
        {
            "name": "w",
            "tasks": {
                "a": {"tool": "pg_query", "args": {"query": "x"}},
                "b": {"tool": "pg_query", "args": {"query": "y"}},
                "c": {"tool": "ch_query", "args": {"query": "{{ a }} union {{ b }}"}},
            },
            "edges": ["[a.result, b.result] -> c.args.query"],
        }
    )

    (binding,) = graph.bindings_of("c")
    assert binding.sources == ("a", "b")
    assert binding.template == "{{ a }} union {{ b }}"
    assert graph.args_of("c", {"a": "1", "b": "2"}) == {"query": "1 union 2"}
    assert graph.stages[-1].tasks == ("c",)


def test_unset_argument_takes_the_source_whole() -> None:
    graph = build(
        {
            "name": "w",
            "tasks": {
                "a": {"tool": "pg_query", "args": {"query": "x"}},
                "c": {"tool": "ch_query"},
            },
            "edges": ["a.result -> c.args.query"],
        }
    )

    (binding,) = graph.bindings_of("c")
    assert binding.template == ""
    assert graph.args_of("c", {"a": "rows"}) == {"query": "rows"}
    assert graph.bindings_of("a") == ()


def test_module_tool_ports() -> None:
    graph = build(
        {
            "name": "w",
            "tasks": {
                "copy": {"tool": "pg_copy_out", "args": {"query": "select 1"}},
                "insert": {"tool": "ch_insert", "args": {"table": "t"}},
            },
            "edges": ["copy.out -> insert.src"],
        }
    )

    assert len(graph.stages) == 1
    assert graph.stages[0].tasks == ("copy", "insert")


@pytest.mark.parametrize(
    ("raw", "codes"),
    [
        (
            {"name": "w", "tasks": {"a": {"tool": "nope"}}},
            [IssueCode.UNKNOWN_TOOL],
        ),
        (
            {"name": "w", "tasks": {"a": {"tool": "secret_tool"}}},
            [IssueCode.TOOL_DENIED],
        ),
        (
            {
                "name": "w",
                "tasks": {"a": {"tool": "canvas_open", "args": {"path": "x"}}},
            },
            [IssueCode.TOOL_CHAT_ONLY],
        ),
        (
            {"name": "w", "tasks": {"a": {"tool": "pg_query", "args": {"sql": "x"}}}},
            [IssueCode.UNKNOWN_ARG, IssueCode.MISSING_ARG],
        ),
        (
            {"name": "w", "tasks": {"a": {"tool": "pg_query"}}},
            [IssueCode.MISSING_ARG],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "a": {
                        "tool": "pg_query",
                        "args": {"query": "x"},
                        "ports": {"o": "write"},
                    }
                },
            },
            [IssueCode.PORTS_NOT_ALLOWED],
        ),
        (
            {"name": "w", "tasks": {"a": bash("x")}, "edges": ["a -> b"]},
            [IssueCode.UNKNOWN_TASK],
        ),
        (
            {
                "name": "w",
                "tasks": {"a": bash("x", out="write"), "b": bash("y", src="read")},
                "edges": ["a.nope -> b.src", "a.out -> b.src"],
            },
            [IssueCode.UNKNOWN_PORT],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "copy": {"tool": "pg_copy_out", "args": {"query": "x"}},
                    "insert": {"tool": "ch_insert", "args": {"table": "t"}},
                },
                "edges": ["insert.src -> copy.out"],
            },
            [IssueCode.PORT_DIRECTION, IssueCode.PORT_DIRECTION],
        ),
        (
            {
                "name": "w",
                "tasks": {"a": bash("x", out="write", src="read")},
                "edges": ["a.out -> a.src"],
            },
            [
                IssueCode.SELF_EDGE,
                IssueCode.PORT_UNCONNECTED,
                IssueCode.PORT_UNCONNECTED,
            ],
        ),
        (
            {
                "name": "w",
                "tasks": {"a": bash("x"), "b": bash("y")},
                "edges": ["a -> b", "a -> b"],
            },
            [IssueCode.DUPLICATE_EDGE],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "a": {"tool": "pg_query", "args": {"query": "x"}},
                    "b": {"tool": "pg_query", "args": {"query": "y"}},
                    "c": {"tool": "ch_query"},
                },
                "edges": ["a.result -> c.args.query", "b.result -> c.args.query"],
            },
            [IssueCode.ARG_BOUND_TWICE],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "a": {"tool": "pg_query", "args": {"query": "x"}},
                    "c": {"tool": "ch_query", "args": {"query": "y"}},
                },
                "edges": ["a.result -> c.args.query"],
            },
            [IssueCode.ARG_BOUND_AND_SET],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "a": {"tool": "pg_query", "args": {"query": "x"}},
                    "c": {"tool": "ch_query", "args": {"query": "{{ a "}},
                },
                "edges": ["a.result -> c.args.query"],
            },
            [IssueCode.TEMPLATE_SYNTAX],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "a": {"tool": "pg_query", "args": {"query": "x"}},
                    "c": {"tool": "ch_query", "args": {"query": "{{ a }} {{ b }}"}},
                },
                "edges": ["a.result -> c.args.query"],
            },
            [IssueCode.TEMPLATE_UNBOUND],
        ),
        (
            {"name": "w", "tasks": {"a": bash("x", out="write")}},
            [IssueCode.PORT_UNCONNECTED],
        ),
        (
            {
                "name": "w",
                "tasks": {"a": bash("x", out="write"), "b": bash("y", src="read")},
                "edges": ["a.out -> b.src", "a -> b"],
            },
            [IssueCode.STAGE_DEADLOCK],
        ),
        (
            {
                "name": "w",
                "tasks": {"a": bash("x"), "b": bash("y"), "c": bash("z")},
                "edges": ["a -> b", "b -> c", "c -> a"],
            },
            [IssueCode.CYCLE, IssueCode.CYCLE],
        ),
        (
            {
                "name": "w",
                "tasks": {
                    "a": bash("x", out="write", src="read"),
                    "b": bash("y", out="write", src="read"),
                },
                "edges": ["a.out -> b.src", "b.out -> a.src"],
            },
            [IssueCode.CYCLE],
        ),
    ],
)
def test_issue(raw: dict[str, Any], codes: list[IssueCode]) -> None:
    assert issues_of(raw) == codes


def test_structural_issues_are_collected_across_tasks_and_edges() -> None:
    codes = issues_of(
        {
            "name": "w",
            "tasks": {"a": {"tool": "nope"}, "b": {"tool": "pg_query"}},
            "edges": ["a -> c"],
        }
    )

    assert codes == [
        IssueCode.UNKNOWN_TOOL,
        IssueCode.UNKNOWN_TASK,
        IssueCode.MISSING_ARG,
    ]
