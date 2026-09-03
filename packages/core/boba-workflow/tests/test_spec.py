"""Разбор спеки: порты, рёбра, списки, замечания, YAML туда и обратно."""

from __future__ import annotations

import pytest

from boba.workflow import (
    ArgTemplate,
    EdgeKind,
    EdgeText,
    IssueCode,
    PortDirection,
    PortKind,
    PortRef,
    WorkflowSpec,
    WorkflowSpecError,
)

EXAMPLE = """
name: pg-to-ch
description: copy batch to clickhouse and verify
tasks:
  dump:
    tool: bash
    ports: {out: write}
    args: {command: "psql -c 'copy t to stdout csv' > $out"}
  load:
    tool: bash
    ports: {src: read}
    args: {command: "clickhouse-client --query 'insert into t format CSV' < $src"}
  count:
    tool: bash
    ports: {src: read}
    args: {command: "wc -l < $src"}
  ids:
    tool: pg_query
    args: {query: "select id from batches"}
  check:
    tool: ch_query
    args: {query: "select count() from t where id in ({{ ids }})"}
edges:
  - dump.out -> load.src
  - dump.out -> count.src
  - ids.result -> check.args.query
  - load -> check
"""


def test_example_parses_with_kinds() -> None:
    spec = WorkflowSpec.parse_yaml(EXAMPLE)

    assert spec.name == "pg-to-ch"
    assert set(spec.tasks) == {"dump", "load", "count", "ids", "check"}
    assert spec.tasks["dump"].ports == {"out": PortDirection.WRITE}

    kinds = [edge.kind for edge in spec.edges]
    assert kinds == [
        EdgeKind.STREAM,
        EdgeKind.STREAM,
        EdgeKind.VALUE,
        EdgeKind.CONTROL,
    ]

    value = spec.edges[2]
    assert value.src == PortRef(task="ids", kind=PortKind.RESULT)
    assert value.dst == PortRef(task="check", kind=PortKind.ARG, name="query")


def test_port_render_round_trip() -> None:
    for text in ("a", "a.result", "a.args.query", "a.out"):
        assert PortRef.parse(text).render() == text


def test_edge_list_expands_to_product() -> None:
    edges = EdgeText.parse("[b, c] -> [d, e]")

    rendered = [edge.render() for edge in edges]
    assert rendered == ["b -> d", "b -> e", "c -> d", "c -> e"]
    assert {edge.kind for edge in edges} == {EdgeKind.CONTROL}


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("a -> b -> c", IssueCode.EDGE_SYNTAX),
        ("a ->", IssueCode.EDGE_SYNTAX),
        ("[a, b -> c", IssueCode.EDGE_SYNTAX),
        ("a.b.c.d -> e", IssueCode.PORT_SYNTAX),
        ("a.x.y -> e", IssueCode.PORT_SYNTAX),
        ("1a -> b", IssueCode.PORT_SYNTAX),
        ("a.result -> b.src", IssueCode.EDGE_KIND),
        ("a.out -> b", IssueCode.EDGE_KIND),
        ("a -> b.args.x", IssueCode.EDGE_KIND),
    ],
)
def test_bad_edge_text(text: str, code: IssueCode) -> None:
    with pytest.raises(WorkflowSpecError) as caught:
        EdgeText.parse(text)

    assert [issue.code for issue in caught.value.issues] == [code]


def test_issues_of_all_edges_are_collected() -> None:
    raw = {
        "name": "w",
        "tasks": {"a": {"tool": "bash"}},
        "edges": ["a ->", "a.result -> b.src", "a -> a"],
    }

    with pytest.raises(WorkflowSpecError) as caught:
        WorkflowSpec.parse(raw)

    codes = [issue.code for issue in caught.value.issues]
    assert codes == [IssueCode.EDGE_SYNTAX, IssueCode.EDGE_KIND]


def test_schema_issues_name_the_field() -> None:
    raw = {"name": "bad name", "tasks": {}, "extra": 1}

    with pytest.raises(WorkflowSpecError) as caught:
        WorkflowSpec.parse(raw)

    codes = {issue.code for issue in caught.value.issues}
    wheres = {issue.where for issue in caught.value.issues}
    assert codes == {IssueCode.SCHEMA}
    # пустой tasks валиден: New создаёт workflow без задач
    assert wheres == {"name", "extra"}


def test_yaml_error_is_one_issue() -> None:
    with pytest.raises(WorkflowSpecError) as caught:
        WorkflowSpec.parse_yaml("tasks: [unclosed")

    assert [issue.code for issue in caught.value.issues] == [IssueCode.YAML]


def test_render_yaml_round_trip() -> None:
    spec = WorkflowSpec.parse_yaml(EXAMPLE)

    again = WorkflowSpec.parse_yaml(spec.render_yaml())

    assert again == spec
    assert "dump.out -> load.src" in spec.render_yaml()


def test_arg_template_names_and_render() -> None:
    assert ArgTemplate.names_of("in ({{ ids }}) and {{a}}") == {"ids", "a"}
    assert ArgTemplate.names_of("plain {x}") == frozenset()
    assert ArgTemplate.render("in ({{ ids }})", {"ids": r"a\1"}) == r"in (a\1)"
    assert ArgTemplate.render("{{ a }}|{{ b }}", {"a": "1", "b": "2"}) == "1|2"


def test_arg_template_errors() -> None:
    with pytest.raises(WorkflowSpecError) as caught:
        ArgTemplate.names_of("{{ x ")

    assert [issue.code for issue in caught.value.issues] == [IssueCode.TEMPLATE_SYNTAX]

    with pytest.raises(WorkflowSpecError):
        ArgTemplate.render("{{ x }}", {})

    with pytest.raises(WorkflowSpecError):
        ArgTemplate.render("{{ x.__class__ }}", {"x": "1"})
