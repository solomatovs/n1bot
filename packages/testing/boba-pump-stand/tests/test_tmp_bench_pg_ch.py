# ruff: noqa
"""Временный замер pg_stream_out -> ch_stream_in; удалить после замера."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from boba.pump_stand import ClickHouseSide, PostgresSide, PumpDags, PumpStand
from boba.stand.names import StandNames

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
SRC = StandNames().of("bench_pg")
DW = StandNames().of("bench_dw")
ROWS = 4_000_000

SQL = (
    "select g as id, 'customer number ' || g as name, (g * 1.5)::float8 as amount, "
    "'city ' || (g % 500) as city, repeat('x', g % 40) as note, g % 2 = 0 as flag, "
    f"g % 1000 as qty from generate_series(1, {ROWS}) g"
)

SPEC = """
name = "bench_$wire"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$sql'''
wire = "$wire"
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "bench_$wire"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
order_by = "id"
feed = "rows"
"""

DRAIN = """
name = "drain_$wire"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$sql'''
wire = "$wire"
out = "rows"

[[nodes]]
key = "sink"
tool = "dev_null"

[nodes.args]
feeds = ["rows"]
"""


def _pick(sources: Any, name: str) -> list[Any]:
    return [source for source in sources if source.name == name][:1]


@pytest.fixture(scope="module", params=_pick(STAND.sources, "pg-17"), ids=lambda s: s.name)
async def source(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, SRC)
    await side.connect()
    yield side


@pytest.fixture(scope="module", params=STAND.demo_clickhouse()[-1:], ids=lambda s: s.name)
async def target(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, DW)
    await side.connect()
    await side.recreate_database()
    yield side
    await side.drop()


@pytest.mark.parametrize("wire", ["arrow", "tsv"])
@pytest.mark.parametrize("spec", ["drain", "load"])
async def test_bench(
    tmp_path: Path, source: PostgresSide, target: ClickHouseSide, wire: str, spec: str
) -> None:
    dags = PumpDags(
        tmp_path,
        {"pg": source.profile, "ch": target.profile},
        {"dw": DW, "sql": SQL, "wire": wire},
    )
    text = {"drain": DRAIN, "load": SPEC}[spec]
    started = time.perf_counter()
    outcome = await dags.run(text)
    spent = time.perf_counter() - started
    assert outcome.ok(), [node.content for node in outcome.nodes]
    print(f"\nBENCH {spec:<5} {wire:<5} {ROWS} rows in {spent:6.2f} s = {ROWS / spent / 1000:6.0f}k rows/s")
