# ruff: noqa: S608
"""Модель связывает насосы каналами: postgres раздаётся в ClickHouse и в
postgres одним ответом.

Вызовы идут путём чата: вызовы ответа модели отдаются StreamGroups до
исполнения, затем все исполняются одновременно полными ToolCall, как их
зовёт ToolNode; тела — настоящие процессы инструментов pg и ch на базах
стенда.

Что проверяется:
    - один выход pg_stream_out по arrow читают ch_stream_in и pg_stream_in,
      обе таблицы совпадают с источником;
    - приёмник postgres отказал: группа срывается, ClickHouse не меняет
      таблицу (exchange tables стоит за барьером группы).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from boba.pump_stand import ClickHouseSide, PostgresSide, PumpStand
from boba.runtime.plugins import ToolBridge
from boba.tool.ch import tools as ch
from boba.tool.pg import tools as pg
from boba.toolkit.chain import StreamTimings
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import (
    FailureResult,
    GroupCall,
    GroupFailureResult,
    ToolArtifact,
)
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.stream_calls import StreamCallHooks, StreamChannelFields, StreamGroups

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = "pump_channels"
CH_DATABASE = "pump_channels"
ROWS = 20000
TIMINGS = StreamTimings(open_sec=60.0, stall_sec=120.0, poll_sec=0.2)
MERGE_TREE = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree order by {order_by}"
)


def _newest_postgres() -> Any:
    newest: Any = None
    for source in STAND.sources:
        if source.name.startswith("pg-"):
            newest = source

    if newest is None:
        raise AssertionError("no postgres source on the stand")

    return newest


@pytest.fixture(scope="module")
async def postgres() -> AsyncIterator[PostgresSide]:
    side = PostgresSide(_newest_postgres(), PG_SCHEMA)
    await side.connect()
    await side.recreate_schema(
        [
            "create table src (id bigint not null, note text, amount numeric(18,4))",
            (
                "insert into src select g, 'строка ' || g, g * 1.5 "
                f"from generate_series(1, {ROWS}) g"
            ),
        ]
    )

    yield side

    await side.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(STAND.demo_clickhouse()[0], CH_DATABASE)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


class ChannelTools:
    """pg и ch насосы под обвязками чата: поля каналов, роль по tool_call_id,
    обёртка запуска субпроцессом."""

    def __init__(self, workdir: Path) -> None:
        cfg = ProcessLauncherConfig(
            provider="process",
            workdir=str(workdir),
            timeout_sec=300.0,
            channel_limit_bytes=8_000_000,
            stderr_tail_bytes=16384,
            kill_grace_sec=1.0,
        )
        launcher = ProcessToolCaller("pump-channels", cfg)

        tools: list[Any] = []
        for payload in (pg.pg_stream_out, pg.pg_stream_in, ch.ch_stream_in):
            tools.append(ToolBridge.as_structured_tool(payload.model_copy()))

        ToolProcessWrap.guard_all(ToolMain.toolset(*tools), launcher)
        StreamChannelFields().attach_all(tools)
        ToolCallIdField.attach_all(tools)
        self._streams = StreamGroups(TIMINGS, tools)
        StreamCallHooks(self._streams).guard_all(tools)
        ToolErrorGuard().guard_all(tools)

        self._tools: dict[str, Any] = {}
        for tool in tools:
            self._tools[tool.name] = tool

    async def respond(self, calls: Sequence[Mapping[str, Any]]) -> list[Any]:
        """Вызовы одного ответа модели: сначала план, затем все разом."""
        tool_calls: list[Any] = []
        for index, call in enumerate(calls):
            tool_calls.append({**call, "id": f"call_{index}", "type": "tool_call"})

        self._streams.open(tool_calls)

        runs: list[Any] = []
        for call in tool_calls:
            runs.append(self._tools[call["name"]].ainvoke(call))

        messages = await asyncio.gather(*runs)

        artifacts: list[Any] = []
        for message in messages:
            artifacts.append(ToolArtifact.revive(message.artifact))

        return artifacts


def _source(postgres: PostgresSide) -> dict[str, Any]:
    return {
        "name": "pg_stream_out",
        "args": {
            "connection": postgres.profile,
            "sql": f"select id, note, amount from {PG_SCHEMA}.src",
            "wire": "arrow",
            "columns": [{"name": "id", "nullable": False}],
            "out": "rows",
        },
    }


def _pg_sink(postgres: PostgresSide, table: str, schema_kind: str) -> dict[str, Any]:
    return {
        "name": "pg_stream_in",
        "args": {
            "connection": postgres.profile,
            "schema_name": PG_SCHEMA,
            "table_name": table,
            "schema_strategy": {"kind": schema_kind},
            "delete_strategy": {"kind": "nothing"},
            "insert_strategy": {"kind": "full"},
            "feed": "rows",
        },
    }


def _ch_sink(clickhouse: ClickHouseSide, table: str) -> dict[str, Any]:
    return {
        "name": "ch_stream_in",
        "args": {
            "connection": clickhouse.profile,
            "database": CH_DATABASE,
            "table_name": table,
            "schema_strategy": {"kind": "create_if_not_exists"},
            "delete_strategy": {"kind": "truncate"},
            "insert_strategy": {"kind": "full"},
            "create_table": MERGE_TREE,
            "order_by": "id",
            "feed": "rows",
        },
    }


class TestFanOutAcrossEngines:
    async def test_one_postgres_stream_lands_in_clickhouse_and_postgres(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide, tmp_path: Path
    ) -> None:
        tools = ChannelTools(tmp_path)

        results = await tools.respond(
            [
                _source(postgres),
                _ch_sink(clickhouse, "copy"),
                _pg_sink(postgres, "copy", "create_if_not_exists"),
            ]
        )

        for result in results:
            assert not isinstance(result, FailureResult), result

        source = await postgres.select("src", ["id", "note", "amount::text"])
        pg_copy = await postgres.select("copy", ["id", "note", "amount::text"])
        ch_copy = await clickhouse.select("copy", ["id", "note", "toString(amount)"])

        assert len(source) == ROWS
        assert [tuple(row) for row in pg_copy] == [tuple(row) for row in source]

        ch_rows: list[tuple[int, str, str]] = []
        for row_id, note, amount in ch_copy:
            ch_rows.append((int(row_id), str(note), str(amount)))

        expected: list[tuple[int, str, str]] = []
        for row_id, note, amount in source:
            expected.append((int(row_id), str(note), self._trimmed(str(amount))))

        assert ch_rows == expected

    @staticmethod
    def _trimmed(amount: str) -> str:
        """toString(Decimal) ClickHouse печатает без хвостовых нулей."""
        if "." not in amount:
            return amount

        return amount.rstrip("0").rstrip(".")


class TestAllOrNothingAcrossEngines:
    async def test_failed_postgres_sink_keeps_clickhouse_untouched(
        self, postgres: PostgresSide, clickhouse: ClickHouseSide, tmp_path: Path
    ) -> None:
        """Приёмник postgres требует несуществующую таблицу и отказывает:
        ClickHouse дочитал поток, но exchange tables не делает — таблица
        остаётся прежней."""
        await clickhouse.create(
            "kept", ["id Int64", "note String", "amount Decimal(18,4)"]
        )
        await clickhouse.command(
            f"insert into {CH_DATABASE}.kept values (1, 'before', 1.0)"
        )

        tools = ChannelTools(tmp_path)
        results = await tools.respond(
            [
                _source(postgres),
                _ch_sink(clickhouse, "kept"),
                _pg_sink(postgres, "missing", "error_if_not_exists"),
            ]
        )

        for result in results:
            assert isinstance(result, GroupFailureResult), result
            assert result.origin == GroupCall(tool="pg_stream_in", call_id="call_2")
            assert "nothing was committed" in result.llm_view()

        kept = await clickhouse.select("kept", ["id", "note"])
        assert [tuple(row) for row in kept] == [(1, "before")]
