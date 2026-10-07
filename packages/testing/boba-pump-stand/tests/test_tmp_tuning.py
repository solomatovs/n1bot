# ruff: noqa
"""Временная матрица замеров для алгоритма настроек; удалить после замера."""
from __future__ import annotations

import asyncio, time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.pump_stand import ClickHouseSide, PostgresSide, PumpDags, PumpStand
from boba.stand.names import StandNames

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
SRC = StandNames().of("tune_pg")
DW = StandNames().of("tune_dw")
SQL = Path("/mnt/store/tmp/claude-1000/-app-docker-compose-boba/a4dc3de1-11c3-4156-a66c-4cf6635def1b/scratchpad/big_stream.sql").read_text().strip()

SPEC = """
name = "tune"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$sql'''
wire = "$wire"
columns = [{ name = "id", nullable = false }]
copy_options = { chunk_bytes = $chunk }
pipe_bytes = $pipe
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$dw"
table_name = "tune"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
before = $before
feed = "rows"
"""

def sets(**kw: Any) -> str:
    return "[" + ", ".join(f'"SET {k} = {v}"' for k, v in kw.items()) + "]"

MIB = 1 << 20
CASES = [
    # label, wire, chunk, pipe, before
    ("base arrow c256K p64K", "arrow", 262144, 65536, "[]"),
    ("chunk 64K", "arrow", 65536, 65536, "[]"),
    ("chunk 1M", "arrow", 1 << 20, 65536, "[]"),
    ("chunk 4M", "arrow", 4 << 20, 65536, "[]"),
    ("pipe 256K", "arrow", 262144, 262144, "[]"),
    ("pipe 1M", "arrow", 262144, 1 << 20, "[]"),
    ("chunk 1M pipe 1M", "arrow", 1 << 20, 1 << 20, "[]"),
    ("block 8192 rows/1 byte", "arrow", 262144, 65536, sets(min_insert_block_size_rows=8192, min_insert_block_size_bytes=1)),
    ("block 65536/16MiB", "arrow", 262144, 65536, sets(min_insert_block_size_rows=65536, min_insert_block_size_bytes=16 * MIB)),
    ("block 262144/64MiB", "arrow", 262144, 65536, sets(min_insert_block_size_rows=262144, min_insert_block_size_bytes=64 * MIB)),
    ("block 1M/256MiB insert_threads 1", "arrow", 262144, 65536, sets(max_insert_threads=1, max_threads=1)),
    ("block 65536/16MiB threads 1", "arrow", 262144, 65536, sets(min_insert_block_size_rows=65536, min_insert_block_size_bytes=16 * MIB, max_insert_threads=1, max_threads=1)),
    ("tsv base", "tsv", 262144, 65536, "[]"),
    ("tsv block 65536/16MiB", "tsv", 262144, 65536, sets(min_insert_block_size_rows=65536, min_insert_block_size_bytes=16 * MIB)),
    ("tsv chunk 1M pipe 1M", "tsv", 1 << 20, 1 << 20, "[]"),
]


class Watch:
    def __init__(self, profile: Any) -> None:
        self._profile = profile
        self.query_peak = 0
        self.merge_peak = 0
        self._task: Any = None

    async def _one(self, client: Any, text: str) -> int:
        rows = (await client.query(text)).result_rows
        return int(rows[0][0] or 0) if rows else 0

    async def _run(self) -> None:
        async with PayloadClickHouse.opened_config(self._profile) as client:
            while True:
                try:
                    q = await self._one(client, "select max(memory_usage) from system.processes where query ilike 'insert into%input(%'")
                    m = await self._one(client, "select value from system.metrics where metric = 'MergesMutationsMemoryTracking'")
                    self.query_peak = max(self.query_peak, q)
                    self.merge_peak = max(self.merge_peak, m)
                except Exception:
                    pass
                await asyncio.sleep(0.1)

    async def __aenter__(self) -> "Watch":
        self._task = asyncio.ensure_future(self._run())
        return self

    async def __aexit__(self, *a: Any) -> None:
        self._task.cancel()
        try:
            await self._task
        except BaseException:
            pass


def _pick(sources: Any, name: str) -> list[Any]:
    return [source for source in sources if source.name == name][:1]


@pytest.fixture(scope="module", params=_pick(STAND.sources, "pg-17"), ids=lambda s: s.name)
async def source(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, SRC)
    await side.connect()
    yield side


@pytest.fixture(scope="module", params=_pick(STAND.ch_sources, "ch-26.7"), ids=lambda s: s.name)
async def target(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, DW)
    await side.connect()
    await side.recreate_database()
    yield side
    await side.drop()


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
async def test_tuning(tmp_path: Path, source: PostgresSide, target: ClickHouseSide, case: Any) -> None:
    label, wire, chunk, pipe, before = case
    dags = PumpDags(tmp_path, {"pg": source.profile, "ch": target.profile},
                    {"dw": DW, "sql": SQL, "wire": wire, "chunk": str(chunk), "pipe": str(pipe), "before": before})
    watch = Watch(target.profile)
    started = time.perf_counter()
    async with watch:
        outcome = await dags.run(SPEC)
    spent = time.perf_counter() - started
    parts = "?"
    try:
        async with PayloadClickHouse.opened_config(target.profile) as client:
            rows = (await client.query(f"select count(), sum(rows) from system.parts where database = '{DW}' and table = 'tune' and active")).result_rows
            parts = f"{rows[0][0]} parts / {rows[0][1]} rows"
    except Exception as exc:
        parts = str(exc)[:80]
    print(f"\nTUNE {label:<34} ok={outcome.ok()} {spent:5.1f}s {10_000_000 / spent / 1000:5.0f}k rows/s | query peak {watch.query_peak / MIB:7.1f} MiB | merges peak {watch.merge_peak / MIB:7.1f} MiB | {parts}")
    if not outcome.ok():
        print("TUNEFAIL", outcome.node("ch").content[:300].replace("\n", " "))
