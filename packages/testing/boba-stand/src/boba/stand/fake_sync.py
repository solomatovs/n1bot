"""Фейковый инструмент снятия для стендов синхронизации каталога: шлёт кадры
плана, порций и итога по образцу PgSample через тот же выходной порт, что и
pg_schema_snapshot, без базы. Сценарий выбирается аргументом schemas:
FakeSyncScenario перечисляет, что инструмент делает в каждом.

Запускается субпроцессом ToolMain, как настоящие тела инструментов.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from boba.catalog import (
    SourceRecord,
    SourceSnapshot,
    SyncDone,
    SyncEmitter,
    SyncFrame,
    SyncPlan,
)
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.ports import Outbound
from boba.toolkit.result import TextResult, ToolResult, render_for_llm


class FakeSyncScenario(StrEnum):
    """Что делает фейк по значению schemas."""

    SAMPLE = ""
    NEXT = "next"
    SLOW = "slow"
    BROKEN_DONE = "broken-done"
    CRASH = "crash"
    WRONG_KIND = "wrong-kind"

    @classmethod
    def parse(cls, schemas: str) -> FakeSyncScenario:
        return cls(schemas.strip())


class FakeSyncScript:
    """Разбивка снимка образца на порции для отправителя кадров."""

    def __init__(self, snapshot: SourceSnapshot, batch_size: int) -> None:
        self._snapshot = snapshot
        self._batch_size = batch_size

    def plan(self, source_kind: str) -> SyncPlan:
        return SyncPlan(
            source_kind=source_kind,
            database="prod",
            schemas=("public", "etl"),
            objects_total=self._snapshot.objects_count(),
            server_version="fake 17.0",
        )

    def batches(self) -> Iterator[tuple[str, Sequence[SourceRecord]]]:
        for part in self._snapshot.parts():
            records = list(self._snapshot.records_of(part.name))
            for start in range(0, len(records), self._batch_size):
                yield part.name, records[start : start + self._batch_size]


@tool
async def fake_pg_snapshot(
    connection: Annotated[str, Field(description="Имя подключения")],
    schemas: Annotated[str, Field(description="Сценарий FakeSyncScenario")],
    batch_size: Annotated[int, Field(ge=1, description="Записей в порции")],
    pause_ms: Annotated[int, Field(ge=0, description="Пауза между порциями")],
    out: Annotated[Outbound[SyncFrame], Injected],
) -> tuple[str, ToolResult]:
    """Кадры синхронизации по образцу PgSample; сценарий выбирает schemas."""
    scenario = FakeSyncScenario.parse(schemas)
    sample = PgSample()
    snapshot = sample.snapshot()
    if scenario is FakeSyncScenario.NEXT:
        snapshot = sample.next_version()

    source_kind = PgSnapshot.source_kind()
    if scenario is FakeSyncScenario.WRONG_KIND:
        source_kind = "clickhouse"

    script = FakeSyncScript(snapshot, batch_size)
    emitter = SyncEmitter(out, PgSnapshot)
    emitter.plan(script.plan(source_kind))

    if scenario is FakeSyncScenario.CRASH:
        msg = f"fake snapshot of {connection!r} crashed on purpose"
        raise RuntimeError(msg)

    for part, records in script.batches():
        if scenario is FakeSyncScenario.SLOW:
            time.sleep(pause_ms / 1000)

        emitter.batch(part, records)

    if scenario is FakeSyncScenario.BROKEN_DONE:
        out.emit(SyncDone(counts={}, objects_total=999, batches=emitter.batches))
    else:
        emitter.done()

    artifact = TextResult(
        text=f"fake snapshot of {connection!r}: {emitter.batches} batches"
    )
    return render_for_llm(artifact), artifact


TOOLS: Final = ToolMain.toolset(fake_pg_snapshot)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
