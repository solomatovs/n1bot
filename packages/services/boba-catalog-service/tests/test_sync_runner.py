"""Синхронизация подключения на живом Postgres: фейковый инструмент снятия в
субпроцессе кладёт образец PgSample в домен каталога тем же SnapshotWriter,
что и настоящий pg_schema_snapshot; хост по его итогу записывает версию.
Проверяются полный проход, diff между двумя проходами, устаревание привязок
процесса, отмена посреди порций, отказы инструмента и его итога, права и
события шины."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from boba.catalog import ChangeStatus, SourceDiff, StaleReason
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogRefusalError,
    CatalogService,
    ProcessSpec,
    SyncCaller,
    SyncClosedError,
    SyncRunningError,
    SyncScope,
    SyncSetupError,
    SyncStatus,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.catalog import StagingTable
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.context import HumanInitiator, NoUserCredential, Subject
from boba.messaging import ChangeAction
from boba.stand.catalog_ports import FakeConnections, FakeSyncPorts
from boba.stand.catalog_stand import CatalogStand, ChangeCollector
from boba.stand.fake_sync import FakeSyncScenario
from boba.stand_core.context import TEST_PROFILE

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROLE = "editor"
CONFIG = CatalogStand.config("catalog_sync_test", ("viewer",), (ROLE,))
CONNECTION_NAME = "prod-pg"
CONNECTION = FakeConnections.info(CONNECTION_NAME, "postgres")
CONNECTION_ID = CONNECTION.id
CH_CONNECTION = FakeConnections.info("dwh-ch", "clickhouse")


def _subject(user_id: UUID, *roles: str) -> Subject:
    return Subject.of_user(user_id, f"user-{user_id.int}", roles, TEST_PROFILE)


EDITOR = _subject(UUID(int=1), ROLE)
VIEWER = _subject(UUID(int=2), "viewer")


def _caller(subject: Subject) -> SyncCaller:
    return SyncCaller(
        subject=subject,
        initiator=HumanInitiator(via="api"),
        credential=NoUserCredential(reason="the sync stand carries no ticket"),
    )


@pytest.fixture
async def service(
    pool: AsyncPostgresPool, tmp_path: Path, test_postgres: PostgresConfig
) -> CatalogService:
    stand = await CatalogStand.build(pool, CONFIG, CatalogStand.fake_kinds())
    site = stand.fake_site(tmp_path, ROLE, TEST_PROFILE, test_postgres)
    ports = FakeSyncPorts(site, (CONNECTION, CH_CONNECTION), (EDITOR.user_id,))
    return stand.service(ports)


def _scope(scenario: FakeSyncScenario) -> SyncScope:
    schemas: tuple[str, ...] = ()
    if scenario is not FakeSyncScenario.SAMPLE:
        schemas = (scenario.value,)

    return SyncScope(schemas=schemas)


async def _staging_tables(pool: AsyncPostgresPool) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        pattern = StagingTable.pattern_of(CONNECTION_ID)
        return await StagingTable.names_in(cur, CONFIG.db_schema, pattern)


async def test_full_sync_writes_a_version(
    service: CatalogService, pool: AsyncPostgresPool
) -> None:
    collector = ChangeCollector.listen(service, EDITOR)

    started = await service.start_sync(
        _caller(EDITOR), CONNECTION_ID, _scope(FakeSyncScenario.SAMPLE)
    )
    assert started.status is SyncStatus.RUNNING
    assert started.connection_name == CONNECTION_NAME
    assert started.kind == "postgres"
    assert started.scope.schemas == ()

    done = await service.syncs.wait(started.id)
    assert done.status is SyncStatus.DONE, done.error
    assert done.version == 1
    assert done.objects_total == PgSample().snapshot().objects_count()
    assert done.objects_done == done.objects_total
    assert done.finished_at is not None

    version = await service.connection_versions(EDITOR, CONNECTION_ID)
    assert [item.version for item in version] == [1]
    assert version[0].sync_id == started.id
    assert version[0].connection_name == CONNECTION_NAME
    assert version[0].server_version == "fake 17.0"

    snapshot = await service.connection_snapshot(EDITOR, CONNECTION_ID, 1)
    assert snapshot.objects_count() == PgSample().snapshot().objects_count()
    diff = SourceDiff.between(CONNECTION_ID, snapshot, PgSample().snapshot())
    assert diff.entries == ()

    assert await _staging_tables(pool) == []

    listed = await service.connection_syncs(VIEWER, CONNECTION_ID)
    assert [item.id for item in listed] == [started.id]
    synced = await service.synced_connections(VIEWER)
    assert [(item.name, item.latest_version) for item in synced] == [
        (CONNECTION_NAME, 1)
    ]

    sync_events: list[ChangeAction] = []
    for message in collector.seen:
        if message.sync_id == started.id:
            sync_events.append(message.action)

    assert sync_events[0] is ChangeAction.CREATED
    assert sync_events[-1] is ChangeAction.UPDATED
    connection_events: list[ChangeAction] = []
    for message in collector.seen:
        if message.connection_id == CONNECTION_ID:
            connection_events.append(message.action)

    assert connection_events[-1] is ChangeAction.UPDATED


async def test_second_sync_yields_a_diff_and_stale_pins(
    service: CatalogService,
) -> None:
    first = await service.start_sync(
        _caller(EDITOR), CONNECTION_ID, _scope(FakeSyncScenario.SAMPLE)
    )
    assert (await service.syncs.wait(first.id)).status is SyncStatus.DONE

    created = await service.create_process(EDITOR, ProcessSpec(name="orders"))
    draft = await service.create_draft(EDITOR, created.id, "process")
    process = ProcessSample(CONNECTION_ID)
    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)

    second = await service.start_sync(
        _caller(EDITOR), CONNECTION_ID, _scope(FakeSyncScenario.NEXT)
    )
    finished = await service.syncs.wait(second.id)
    assert finished.status is SyncStatus.DONE, finished.error
    assert finished.version == 2

    diff = await service.connection_diff(EDITOR, CONNECTION_ID, 1, 2)
    removed: list[str] = []
    for entry in diff.entries:
        if entry.status is ChangeStatus.REMOVED:
            removed.append(entry.ref.path[-1])

    assert "customers" in removed

    staleness = await service.staleness(EDITOR, created.id)
    reasons: set[StaleReason] = set()
    for item in staleness.entries:
        if item.connection_id == CONNECTION_ID:
            reasons.add(item.reason)

    assert StaleReason.OBJECT_REMOVED in reasons


async def test_cancel_stops_the_tool_and_leaves_no_version(
    service: CatalogService, pool: AsyncPostgresPool
) -> None:
    started = await service.start_sync(
        _caller(EDITOR),
        CONNECTION_ID,
        _scope(FakeSyncScenario.SLOW),
    )

    for _ in range(100):
        if await _staging_tables(pool):
            break

        await asyncio.sleep(0.1)

    # staging инструмента — по таблице на часть снимка в схеме домена
    tables = await _staging_tables(pool)
    assert len(tables) == len(PgSnapshot.parts())

    cancelled = await service.cancel_sync(EDITOR, started.id)
    assert cancelled.status is SyncStatus.CANCELLED
    assert cancelled.error == "cancelled by the user"
    assert await service.connection_versions(EDITOR, CONNECTION_ID) == []

    with pytest.raises(SyncClosedError):
        await service.cancel_sync(EDITOR, started.id)

    again = await service.start_sync(
        _caller(EDITOR), CONNECTION_ID, _scope(FakeSyncScenario.SAMPLE)
    )
    assert (await service.syncs.wait(again.id)).status is SyncStatus.DONE
    assert await _staging_tables(pool) == []


async def test_only_one_sync_per_connection_runs_at_a_time(
    service: CatalogService,
) -> None:
    started = await service.start_sync(
        _caller(EDITOR),
        CONNECTION_ID,
        _scope(FakeSyncScenario.SLOW),
    )

    with pytest.raises(SyncRunningError):
        await service.start_sync(
            _caller(EDITOR), CONNECTION_ID, _scope(FakeSyncScenario.SAMPLE)
        )

    await service.cancel_sync(EDITOR, started.id)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (FakeSyncScenario.CRASH, "crashed on purpose"),
        (FakeSyncScenario.BROKEN_OUTCOME, "carries no valid version"),
    ],
)
async def test_failures_close_the_sync_with_the_reason(
    service: CatalogService,
    scenario: FakeSyncScenario,
    expected: str,
) -> None:
    started = await service.start_sync(_caller(EDITOR), CONNECTION_ID, _scope(scenario))
    failed = await service.syncs.wait(started.id)

    assert failed.status is SyncStatus.FAILED
    assert failed.error is not None
    assert expected in failed.error
    assert await service.connection_versions(EDITOR, CONNECTION_ID) == []


async def test_setup_refusals(service: CatalogService) -> None:
    with pytest.raises(CatalogRefusalError):
        await service.start_sync(
            _caller(VIEWER), CONNECTION_ID, _scope(FakeSyncScenario.SAMPLE)
        )

    with pytest.raises(SyncSetupError, match="not visible"):
        await service.start_sync(_caller(EDITOR), uuid4(), SyncScope())

    with pytest.raises(SyncSetupError, match="declare no sync tool"):
        await service.start_sync(_caller(EDITOR), CH_CONNECTION.id, SyncScope())

    stranger = _subject(UUID(int=9), ROLE)
    with pytest.raises(SyncSetupError, match="not visible"):
        await service.start_sync(_caller(stranger), CONNECTION_ID, SyncScope())
