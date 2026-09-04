"""Синхронизация источника на живом Postgres: фейковый инструмент снятия в
субпроцессе шлёт кадры образца PgSample тем же каналом, что и настоящий
pg_schema_snapshot; хост складывает порции в staging и переносит версию
одной транзакцией. Проверяются полный проход, diff между двумя проходами,
устаревание привязок процесса, отмена посреди порций, отказы инструмента и
кадров, права и события шины."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from boba.catalog import ChangeStatus, SourceDiff, SourceKinds, StaleReason
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogRefusalError,
    CatalogService,
    CatalogStore,
    ConnectionInfo,
    SourceCreate,
    SourceStore,
    StagingTable,
    SyncCaller,
    SyncClosedError,
    SyncConnectionNotBoundError,
    SyncRequest,
    SyncRunningError,
    SyncScope,
    SyncSetupError,
    SyncStatus,
)
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.context import HumanInitiator, NoUserCredential, Scope, Subject
from boba.messaging import CatalogChanged, ChangeAction, Envelope, MemoryMessageBus
from boba.stand.catalog_ports import FakeSyncPorts
from boba.stand.context import TEST_PROFILE
from boba.stand.fake_sync import FakeSyncScenario

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)
SCHEMA = "catalog_sync_test"
ROLE = "editor"
CONNECTION_ID = UUID(int=77)
CONNECTION_NAME = "prod-pg"
CONNECTION = ConnectionInfo(id=CONNECTION_ID, name=CONNECTION_NAME, kind="postgres")
CH_CONNECTION = ConnectionInfo(id=UUID(int=78), name="dwh-ch", kind="clickhouse")


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=(ROLE,)
    )


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


class FakeKindSnapshot(PgSnapshot):
    """Снимок вида postgres, чей инструмент снятия — фейк стенда."""

    SYNC_TOOL = "fake_pg_snapshot"


@pytest.fixture
async def service(pool: AsyncPostgresPool, tmp_path: Path) -> CatalogService:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    store = CatalogStore(_config(), pool)
    await store.setup()

    kinds = SourceKinds.of(FakeKindSnapshot, ChSnapshot)
    sources = SourceStore(_config(), kinds, pool)
    await sources.setup()

    ports = FakeSyncPorts(
        tmp_path, ROLE, TEST_PROFILE, (CONNECTION, CH_CONNECTION), (EDITOR.user_id,)
    )
    return CatalogService(store, sources, _config(), MemoryMessageBus("test:0"), ports)


@pytest.fixture
async def source_id(service: CatalogService) -> UUID:
    """Источник prod с привязанным подключением, без версий."""
    source = await service.create_source(
        EDITOR, SourceCreate(name="prod", connection_id=CONNECTION_ID)
    )
    return source.id


def _request(scenario: FakeSyncScenario, **scope: int) -> SyncRequest:
    schemas: tuple[str, ...] = ()
    if scenario is not FakeSyncScenario.SAMPLE:
        schemas = (scenario.value,)

    return SyncRequest(
        connection_id=CONNECTION_ID, scope=SyncScope(schemas=schemas, **scope)
    )


class Collector:
    """Подписчик области пользователя: копит CatalogChanged."""

    def __init__(self) -> None:
        self.seen: list[CatalogChanged] = []

    async def __call__(self, envelope: Envelope) -> None:
        if isinstance(envelope.message, CatalogChanged):
            self.seen.append(envelope.message)


def _listen(service: CatalogService, subject: Subject) -> Collector:
    bus = service.bus
    assert isinstance(bus, MemoryMessageBus)

    collector = Collector()
    bus.subscribe(Scope.user(subject.user_id), collector)
    return collector


async def _staging_tables(pool: AsyncPostgresPool) -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = %s and table_name like %s escape '\\'",
            (SCHEMA, StagingTable.PREFIX.replace("_", "\\_") + "%"),
        )
        rows = await cur.fetchall()

    names: list[str] = []
    for row in rows:
        names.append(str(row[0]))

    return names


async def test_full_sync_writes_a_version_from_batches(
    service: CatalogService, source_id: UUID, pool: AsyncPostgresPool
) -> None:
    collector = _listen(service, EDITOR)

    started = await service.start_sync(
        _caller(EDITOR), source_id, _request(FakeSyncScenario.SAMPLE, batch_size=2)
    )
    assert started.status is SyncStatus.RUNNING
    assert started.scope.batch_size == 2

    done = await service.syncs.wait(started.id)
    assert done.status is SyncStatus.DONE, done.error
    assert done.version == 1
    assert done.objects_total == PgSample().snapshot().objects_count()
    assert done.objects_done == done.objects_total
    assert done.finished_at is not None

    version = await service.source_versions(EDITOR, source_id)
    assert [item.version for item in version] == [1]
    assert version[0].sync_id == started.id
    assert version[0].connection_id == CONNECTION_ID
    assert version[0].server_version == "fake 17.0"

    snapshot = await service.source_snapshot(EDITOR, source_id, 1)
    assert snapshot.objects_count() == PgSample().snapshot().objects_count()
    assert SourceDiff.between(source_id, snapshot, PgSample().snapshot()).entries == ()

    assert await _staging_tables(pool) == []

    listed = await service.source_syncs(VIEWER, source_id)
    assert [item.id for item in listed] == [started.id]

    sync_events: list[ChangeAction] = []
    for message in collector.seen:
        if message.sync_id == started.id:
            sync_events.append(message.action)

    assert sync_events[0] is ChangeAction.CREATED
    assert sync_events.count(ChangeAction.UPDATED) >= 3
    source_events: list[ChangeAction] = []
    for message in collector.seen:
        if message.source_id == source_id:
            source_events.append(message.action)

    assert source_events[-1] is ChangeAction.UPDATED


async def test_second_sync_yields_a_diff_and_stale_pins(
    service: CatalogService, source_id: UUID
) -> None:
    first = await service.start_sync(
        _caller(EDITOR), source_id, _request(FakeSyncScenario.SAMPLE)
    )
    assert (await service.syncs.wait(first.id)).status is SyncStatus.DONE

    draft = await service.create_draft(EDITOR, "process")
    process = ProcessSample(source_id)
    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)

    second = await service.start_sync(
        _caller(EDITOR), source_id, _request(FakeSyncScenario.NEXT, batch_size=3)
    )
    finished = await service.syncs.wait(second.id)
    assert finished.status is SyncStatus.DONE, finished.error
    assert finished.version == 2

    diff = await service.source_diff(EDITOR, source_id, 1, 2)
    removed: list[str] = []
    for entry in diff.entries:
        if entry.status is ChangeStatus.REMOVED:
            removed.append(entry.ref.path[-1])

    assert "customers" in removed

    staleness = await service.staleness(EDITOR)
    reasons: set[StaleReason] = set()
    for item in staleness.entries:
        if item.source_id == source_id:
            reasons.add(item.reason)

    assert StaleReason.OBJECT_REMOVED in reasons


async def test_cancel_stops_the_tool_and_drops_staging(
    service: CatalogService, source_id: UUID, pool: AsyncPostgresPool
) -> None:
    started = await service.start_sync(
        _caller(EDITOR),
        source_id,
        _request(FakeSyncScenario.SLOW, batch_size=1, pause_ms=400),
    )

    async def staged() -> bool:
        current = await service.sync(EDITOR, started.id)
        return current.objects_done > 0

    for _ in range(100):
        if await staged():
            break

        await asyncio.sleep(0.1)

    assert await _staging_tables(pool) == [f"sync_{started.id.hex}"]

    cancelled = await service.cancel_sync(EDITOR, started.id)
    assert cancelled.status is SyncStatus.CANCELLED
    assert cancelled.error == "cancelled by the user"
    assert await _staging_tables(pool) == []
    assert await service.source_versions(EDITOR, source_id) == []

    with pytest.raises(SyncClosedError):
        await service.cancel_sync(EDITOR, started.id)

    again = await service.start_sync(
        _caller(EDITOR), source_id, _request(FakeSyncScenario.SAMPLE)
    )
    assert (await service.syncs.wait(again.id)).status is SyncStatus.DONE


async def test_only_one_sync_per_source_runs_at_a_time(
    service: CatalogService, source_id: UUID
) -> None:
    started = await service.start_sync(
        _caller(EDITOR),
        source_id,
        _request(FakeSyncScenario.SLOW, batch_size=1, pause_ms=200),
    )

    with pytest.raises(SyncRunningError):
        await service.start_sync(
            _caller(EDITOR), source_id, _request(FakeSyncScenario.SAMPLE)
        )

    await service.cancel_sync(EDITOR, started.id)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (FakeSyncScenario.CRASH, "crashed on purpose"),
        (FakeSyncScenario.BROKEN_DONE, "sync done declares 0 records of part"),
        (FakeSyncScenario.WRONG_KIND, "the tool reports 'clickhouse' source"),
    ],
)
async def test_failures_close_the_sync_with_the_reason(
    service: CatalogService,
    source_id: UUID,
    pool: AsyncPostgresPool,
    scenario: FakeSyncScenario,
    expected: str,
) -> None:
    started = await service.start_sync(_caller(EDITOR), source_id, _request(scenario))
    failed = await service.syncs.wait(started.id)

    assert failed.status is SyncStatus.FAILED
    assert failed.error is not None
    assert expected in failed.error
    assert await _staging_tables(pool) == []
    assert await service.source_versions(EDITOR, source_id) == []


async def test_setup_refusals(service: CatalogService, source_id: UUID) -> None:
    with pytest.raises(CatalogRefusalError):
        await service.start_sync(
            _caller(VIEWER), source_id, _request(FakeSyncScenario.SAMPLE)
        )

    with pytest.raises(SyncConnectionNotBoundError):
        await service.start_sync(
            _caller(EDITOR),
            source_id,
            SyncRequest(connection_id=uuid4(), scope=SyncScope()),
        )

    no_tool = await service.create_source(
        EDITOR, SourceCreate(name="events", connection_id=CH_CONNECTION.id)
    )
    with pytest.raises(SyncSetupError, match="declare no sync tool"):
        await service.start_sync(
            _caller(EDITOR),
            no_tool.id,
            SyncRequest(connection_id=CH_CONNECTION.id, scope=SyncScope()),
        )

    stranger = _subject(UUID(int=9), ROLE)
    with pytest.raises(SyncSetupError, match="not visible"):
        await service.start_sync(
            _caller(stranger),
            source_id,
            SyncRequest(connection_id=CONNECTION_ID, scope=SyncScope()),
        )
