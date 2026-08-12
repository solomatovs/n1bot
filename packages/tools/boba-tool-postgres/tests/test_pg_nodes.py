"""Узлы postgres: обогащение args, направление COPY, вырожденный граф фасада."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from boba.tool.pg import PgExecutorConfig, build_pg_tools
from boba.tool.pg.caller import PgCaller
from boba.tool.pg.protocol import (
    PgCopyDirection,
    PgCopyFormat,
    PgCopyRequest,
    PgQueryRequest,
    PgStage,
)
from boba.tool.pg.stages import PgCopyNode, PgQueryNode
from boba.toolkit.channels import ChannelSink, StreamCodec
from boba.toolkit.result import ErrorResult, PgCopyTextResult, TableResult, ToolArtifact
from boba.toolkit.sql import UnknownConnectionError
from boba.toolkit.workflow import WorkflowOutcome, WorkflowSpec

PROFILE: dict[str, Any] = {
    "host": "h",
    "port": 5432,
    "dbname": "db",
    "user": "u",
    "password": "s3cret",
}


def pg_config() -> PgExecutorConfig:
    return PgExecutorConfig.model_validate(
        {"profiles": {"main": PROFILE}, "max_rows": 7, "max_bytes": 1000}
    )


class _StageLauncher:
    """Исполнитель графа из одного узла: байты в приёмник, квитанция в итог."""

    def __init__(self, payload: bytes, trailer: Mapping[str, Any]) -> None:
        self._payload = payload
        self._trailer = trailer
        self.spec: WorkflowSpec | None = None

    def call(
        self,
        spec: WorkflowSpec,
        sinks: Mapping[str, ChannelSink] | None = None,
    ) -> WorkflowOutcome:
        self.spec = spec
        stage = spec.nodes[0].id

        if sinks is not None:
            sink = sinks[stage]
            sink.feed(self._payload)
            sink.close()

        return WorkflowOutcome(stages=(), trailers={stage: dict(self._trailer)})


def _rows_trailer(truncated: bool) -> dict[str, Any]:
    return {
        "truncated": truncated,
        "returns_rows": True,
        "rowcount": None,
        "status": "SELECT 1",
    }


def _copy_trailer(rows: int) -> dict[str, Any]:
    return {"direction": PgCopyDirection.TO_STDOUT.value, "rows": rows}


def invoke(tool: Any, args: dict[str, Any]) -> Any:
    """Фасады pg асинхронные; тест держит их вызов в собственном цикле."""
    call = {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
    message = asyncio.run(tool.ainvoke(call))

    return ToolArtifact.revive(message.artifact)




class TestPgEnrichment:
    """Обогатитель добавляет то, чего нет в спеке графа: профиль и потолки."""

    def test_query_args_become_full_request(self) -> None:
        enrich = PgQueryNode.enricher(pg_config())

        enriched = enrich(
            {"connection_name": "main", "sql": "select %s", "params": [1]}
        )
        request = PgQueryRequest.model_validate(enriched)

        assert request.op == PgStage.QUERY
        assert request.row_limit == 7
        assert request.params == (1,)
        assert request.connection.password is not None
        assert request.connection.password.get_secret_value() == "s3cret"

    def test_copy_carries_the_declared_direction(self) -> None:
        enrich = PgCopyNode.enricher(pg_config())

        enriched = enrich(
            {
                "connection_name": "main",
                "direction": PgCopyDirection.TO_STDOUT.value,
                "sql": "COPY (select 1) TO STDOUT WITH (FORMAT CSV, HEADER)",
            }
        )
        request = PgCopyRequest.model_validate(enriched)

        assert request.direction is PgCopyDirection.TO_STDOUT

    def test_copy_from_stdin_is_declared_by_args(self) -> None:
        enrich = PgCopyNode.enricher(pg_config())

        request = PgCopyRequest.model_validate(
            enrich(
                {
                    "connection_name": "main",
                    "direction": PgCopyDirection.FROM_STDIN.value,
                    "sql": "COPY people (id, name) FROM STDIN WITH (FORMAT CSV)",
                }
            )
        )

        assert request.direction is PgCopyDirection.FROM_STDIN

    def test_copy_without_direction_is_rejected(self) -> None:
        enrich = PgCopyNode.enricher(pg_config())

        with pytest.raises(ValidationError):
            enrich({"connection_name": "main", "sql": "COPY people TO STDOUT"})

    def test_statement_is_not_parsed_by_the_node(self) -> None:
        enrich = PgCopyNode.enricher(pg_config())

        enriched = enrich(
            {
                "connection_name": "main",
                "direction": PgCopyDirection.FROM_STDIN.value,
                "sql": "select 1",
            }
        )

        assert PgCopyRequest.model_validate(enriched).sql == "select 1"

    def test_unknown_connection_is_rejected_before_the_stage(self) -> None:
        enrich = PgQueryNode.enricher(pg_config())

        with pytest.raises(UnknownConnectionError):
            enrich({"connection_name": "нет-такого", "sql": "select 1"})

    def test_stray_args_do_not_reach_the_request(self) -> None:
        enrich = PgQueryNode.enricher(pg_config())

        with pytest.raises(ValidationError):
            enrich({"connection_name": "main", "sql": "select 1", "row_limit": 1000})


class TestPgCaller:
    """Одиночный вызов — граф из одного узла; секреты в спеку не попадают."""

    def test_query_spec_has_no_connection_profile(self) -> None:
        launcher = _StageLauncher(b"", _rows_trailer(False))
        caller = PgCaller("pg", lambda tool: launcher)
        collected: list[bytes] = []

        trailer = caller.query(
            connection_name="main",
            sql="select 1",
            params=(),
            sink=_Recorder(collected),
        )

        assert trailer.status == "SELECT 1"

        spec = launcher.spec
        assert spec is not None
        node = spec.nodes[0]
        assert (node.id, node.tool) == (PgStage.QUERY, PgStage.QUERY)
        assert set(node.args) == {"connection_name", "sql", "params"}

    def test_copy_spec_wraps_the_query_into_a_copy_statement(self) -> None:
        launcher = _StageLauncher(b"id\n1\n", _copy_trailer(1))
        caller = PgCaller("pg", lambda tool: launcher)
        collected: list[bytes] = []

        trailer = caller.copy(
            connection_name="main",
            sql="select 1",
            copy_format=PgCopyFormat.CSV,
            sink=_Recorder(collected),
        )

        assert trailer.rows == 1
        assert trailer.direction is PgCopyDirection.TO_STDOUT
        assert b"".join(collected) == b"id\n1\n"

        spec = launcher.spec
        assert spec is not None
        assert spec.nodes[0].tool == PgStage.COPY

        sql = spec.nodes[0].args["sql"]
        assert sql == "COPY (select 1) TO STDOUT WITH (FORMAT CSV, HEADER)"


class _Recorder(ChannelSink):
    """Приёмник, складывающий байты канала для проверки."""

    def __init__(self, sink: list[bytes]) -> None:
        self._sink = sink

    def feed(self, data: bytes) -> None:
        self._sink.append(data)

    def close(self) -> None:
        """Байты уже собраны: закрывать нечего."""


class TestPgFacades:
    """Фасады поверх порта: строки таблицей, выгрузка текстом, отказы результатом."""

    def test_query_returns_rows_of_the_stage(self) -> None:
        payload = StreamCodec.encode_row({"a": 1}) + StreamCodec.encode_row({"a": 2})
        launcher = _StageLauncher(payload, _rows_trailer(False))
        built = build_pg_tools(pg_config(), lambda tool: launcher)
        tool = next(t for t in built if t.name == "pg_query")

        result = invoke(tool, {"connection_name": "main", "sql": "select a"})

        assert isinstance(result, TableResult)
        assert list(result.rows) == [{"a": 1}, {"a": 2}]

    def test_export_asks_for_the_requested_format(self) -> None:
        launcher = _StageLauncher(b"id,name\n1,Ivan\n", _copy_trailer(1))
        built = build_pg_tools(pg_config(), lambda tool: launcher)
        tool = next(t for t in built if t.name == "pg_export")

        result = invoke(
            tool,
            {"connection_name": "main", "sql": "select 1", "copy_format": "csv"},
        )

        assert isinstance(result, PgCopyTextResult)
        assert result.text == "id,name\n1,Ivan\n"

        spec = launcher.spec
        assert spec is not None
        assert "FORMAT CSV" in str(spec.nodes[0].args["sql"])

    def test_export_over_max_bytes_becomes_error_result(self) -> None:
        oversized = b"x" * 2000
        launcher = _StageLauncher(oversized, _copy_trailer(1))
        built = build_pg_tools(pg_config(), lambda tool: launcher)
        tool = next(t for t in built if t.name == "pg_export")

        result = invoke(tool, {"connection_name": "main", "sql": "select 1"})

        assert isinstance(result, ErrorResult)
        assert result.error_kind == "result_too_large"

    def test_unknown_connection_becomes_error_result(self) -> None:
        launcher = _StageLauncher(b"", _rows_trailer(False))
        built = build_pg_tools(pg_config(), lambda tool: launcher)
        tool = next(t for t in built if t.name == "pg_query")

        result = invoke(tool, {"connection_name": "нет-такого", "sql": "select 1"})

        assert isinstance(result, ErrorResult)
        assert result.error_kind == "unknown_target"
