"""Фейковый инструмент снятия для стендов синхронизации каталога: кладёт в
домен каталога снимок образца PgSample тем же SnapshotWriter, что и
pg_schema_snapshot, без базы-источника. Сценарий выбирается аргументом
schemas: FakeSyncScenario перечисляет, что инструмент делает в каждом.
Подключение — только имя, его id стенд выводит из имени (FakeConnection),
как и справочник подключений стенда.

Запускается субпроцессом ToolMain, как настоящие тела инструментов.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final
from uuid import UUID, uuid5

from pydantic import Field

from boba.catalog import SourceSnapshot
from boba.db.postgres import PayloadPostgres
from boba.db.postgres.catalog import (
    CatalogStoreConfig,
    PartTable,
    SnapshotOutcome,
    SnapshotTables,
    SnapshotWriter,
)
from boba.db.postgres.snapshot_sample import PgSample
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import MarkdownResult


class FakeSyncScenario(StrEnum):
    """Что делает фейк по значению schemas."""

    SAMPLE = ""
    NEXT = "next"
    SLOW = "slow"
    BROKEN_OUTCOME = "broken-outcome"
    CRASH = "crash"

    @classmethod
    def parse(cls, schemas: str) -> FakeSyncScenario:
        return cls(schemas.strip())


class FakeConnection:
    """Id подключения стенда по имени: фейк и справочник стенда выводят его
    одинаково, строки соединений у стенда нет."""

    NAMESPACE: ClassVar[UUID] = UUID("6f1b5c2e-0d4a-4b7e-9c3f-1a2b3c4d5e6f")
    SERVER_VERSION: ClassVar[str] = "fake 17.0"
    BATCH: ClassVar[int] = 50
    SLOW_STEP_SEC: ClassVar[float] = 0.5

    @classmethod
    def id_of(cls, name: str) -> UUID:
        return uuid5(cls.NAMESPACE, name)


class FakeSyncScript:
    """Разбивка снимка образца на порции строк по раскладке таблиц домена."""

    def __init__(self, snapshot: SourceSnapshot, batch_size: int) -> None:
        self._snapshot = snapshot
        self._batch_size = batch_size
        self._specs = SnapshotTables.of_snapshot(type(snapshot))

    def tables(self) -> list[PartTable]:
        tables: list[PartTable] = []
        for spec in self._specs:
            tables.append(spec.part_table())

        return tables

    def batches(self) -> Iterator[tuple[str, Sequence[Mapping[str, Any]]]]:
        for spec in self._specs:
            rows = list(spec.rows_of(self._snapshot))
            for start in range(0, len(rows), self._batch_size):
                yield spec.part.name, rows[start : start + self._batch_size]


@tool
async def fake_pg_snapshot(
    connection: Annotated[str, Field(description="Имя подключения")],
    schemas: Annotated[str, Field(description="Сценарий FakeSyncScenario")],
    catalog: Annotated[CatalogStoreConfig, Injected],
) -> MarkdownResult:
    """Снимок образца PgSample в домен каталога; сценарий выбирает schemas."""
    scenario = FakeSyncScenario.parse(schemas)
    sample = PgSample()
    snapshot = sample.snapshot()
    if scenario is FakeSyncScenario.NEXT:
        snapshot = sample.next_version()

    script = FakeSyncScript(snapshot, FakeConnection.BATCH)
    store = await PayloadPostgres.connect_config(catalog.connection)
    async with store:
        writer = SnapshotWriter(
            store, catalog.db_schema, FakeConnection.id_of(connection), script.tables()
        )
        await writer.open()

        if scenario is FakeSyncScenario.CRASH:
            msg = f"fake snapshot of {connection!r} crashed on purpose"
            raise RuntimeError(msg)

        batches = 0
        for part, records in script.batches():
            if scenario is FakeSyncScenario.SLOW:
                time.sleep(FakeConnection.SLOW_STEP_SEC)

            await writer.stage(part, records)
            batches += 1

        version = await writer.commit()

    outcome = SnapshotOutcome(
        version=version, server_version=FakeConnection.SERVER_VERSION
    )
    metadata = outcome.metadata()
    if scenario is FakeSyncScenario.BROKEN_OUTCOME:
        metadata = {}

    return MarkdownResult(
        text=f"fake snapshot of {connection!r}: version {version}, {batches} batches",
        metadata=metadata,
    )


TOOLS: Final = ToolMain.toolset(fake_pg_snapshot)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
