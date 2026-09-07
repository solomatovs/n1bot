"""Хранилище снимков подключений на живом Postgres: запись версии целиком
и чтение обратно байт-в-байт, версии и diff, список синхронизированных
подключений, вид зафиксирован первой версией, версии забываются вместе со
строками."""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    ChangeStatus,
    ObjectKind,
    ObjectRef,
    SourceKinds,
    SourceRecord,
    SourceSnapshot,
)
from boba.catalog_service import (
    CatalogConfig,
    ConnectionNotSyncedError,
    ConnectionStore,
    ConnectionVersionNotFoundError,
    SnapshotKindMismatchError,
    VersionOrigin,
)
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.clickhouse.snapshot_sample import ChSample
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)
"""Реестр видов теста: оба снимка из пакетов драйверов."""

SCHEMA = "catalog_connections_test"
ADMIN = UUID(int=71)
PROD = UUID(int=72)
DWH = UUID(int=73)


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


def _origin(name: str, server_version: str | None = None) -> VersionOrigin:
    return VersionOrigin(
        taken_by=ADMIN, connection_name=name, server_version=server_version
    )


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = ConnectionStore(_config(), KINDS, pool)
    await built.setup()
    await built.setup()
    return built


async def test_postgres_version_round_trips(store: ConnectionStore) -> None:
    sample = PgSample()
    assert await store.synced_connections() == []
    assert await store.synced_or_none(PROD) is None
    with pytest.raises(ConnectionNotSyncedError):
        await store.snapshot_of(PROD, 0)

    version = await store.write_version(
        PROD, sample.snapshot(), _origin("pg-prod", "16.3")
    )
    assert version.version == 1
    assert version.connection_name == "pg-prod"
    assert version.kind == "postgres"
    assert version.objects_total == 9
    assert version.server_version == "16.3"

    synced = await store.synced(PROD)
    assert synced.name == "pg-prod"
    assert synced.kind == "postgres"
    assert synced.latest_version == 1

    stored = await store.snapshot_of(PROD, 1)
    assert isinstance(stored, PgSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    assert await store.snapshot_of(PROD, 0) == PgSnapshot.empty()
    with pytest.raises(ConnectionVersionNotFoundError):
        await store.snapshot_of(PROD, 2)

    # имя подключения на момент снятия: переименованное попадает в новую версию
    second = await store.write_version(
        PROD, sample.next_version(), _origin("pg-prod-main")
    )
    assert second.version == 2
    assert [v.version for v in await store.versions_of(PROD)] == [1, 2]
    assert (await store.synced(PROD)).name == "pg-prod-main"
    latest = await store.latest_snapshot(PROD)
    assert _sorted(latest) == _sorted(sample.next_version())

    diff = await store.diff_of(PROD, 1, 2)
    removed = ObjectRef(
        connection_id=PROD,
        kind=ObjectKind.RELATION,
        path=("prod", "public", "customers"),
    )
    assert diff.status_of(removed) is ChangeStatus.REMOVED
    orders = ObjectRef(
        connection_id=PROD, kind=ObjectKind.RELATION, path=("prod", "public", "orders")
    )
    assert diff.status_of(orders) is ChangeStatus.MODIFIED


async def test_clickhouse_version_round_trips(store: ConnectionStore) -> None:
    sample = ChSample()

    await store.write_version(DWH, sample.snapshot(), _origin("ch-dwh"))
    stored = await store.snapshot_of(DWH, 1)
    assert isinstance(stored, ChSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    tree = stored.children(DWH, ("dwh", "tables"))
    assert [node.label for node in tree] == ["events"]


async def test_snapshot_kind_is_fixed_by_the_first_version(
    store: ConnectionStore,
) -> None:
    await store.write_version(DWH, ChSample().snapshot(), _origin("ch-dwh"))
    with pytest.raises(SnapshotKindMismatchError, match="clickhouse"):
        await store.write_version(DWH, PgSample().snapshot(), _origin("ch-dwh"))


async def test_forget_versions_drops_rows_and_the_listing(
    store: ConnectionStore,
) -> None:
    await store.write_version(PROD, PgSample().snapshot(), _origin("pg-prod"))
    await store.write_version(PROD, PgSample().next_version(), _origin("pg-prod"))
    await store.write_version(DWH, ChSample().snapshot(), _origin("ch-dwh"))
    assert [s.name for s in await store.synced_connections()] == ["ch-dwh", "pg-prod"]

    assert await store.forget_versions(PROD) == 2
    assert await store.forget_versions(PROD) == 0
    assert [s.name for s in await store.synced_connections()] == ["ch-dwh"]
    with pytest.raises(ConnectionNotSyncedError):
        await store.latest_snapshot(PROD)

    # строки снимка ушли каскадом вместе с шапками версий
    again = await store.write_version(PROD, PgSample().snapshot(), _origin("pg-prod"))
    assert again.version == 1
    assert _sorted(await store.snapshot_of(PROD, 1)) == _sorted(PgSample().snapshot())


def _sorted(snapshot: SourceSnapshot) -> dict[str, list[SourceRecord]]:
    """Записи по частям, отсортированные по ключу: порядок строк из базы не
    гарантирован, а содержимое должно совпасть целиком."""
    tables: dict[str, list[SourceRecord]] = {}
    for part in snapshot.parts():
        records = list(snapshot.records_of(part.name))
        records.sort(key=lambda record: record.key)
        tables[part.name] = records

    return tables
