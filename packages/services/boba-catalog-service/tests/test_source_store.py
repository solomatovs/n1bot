"""Хранилище источников на живом Postgres: источники и привязки, запись версии
целиком и чтение обратно байт-в-байт, версии и diff, черновики ручного
источника с порциями, конфликтом, публикацией и устареванием."""

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
    ConnectionAlreadyBoundError,
    SourceKindMismatchError,
    SourceNotFoundError,
    SourceSpec,
    SourceStore,
    SourceVersionNotFoundError,
    VersionOrigin,
)
from boba.db.clickhouse.snapshot import (
    ChSnapshot,
    ChSourceKind,
)
from boba.db.clickhouse.snapshot_sample import ChSample
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot import (
    PgSnapshot,
    PgSourceKind,
)
from boba.db.postgres.snapshot_sample import PgSample

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)
"""Реестр видов теста: оба снимка из пакетов драйверов."""

SCHEMA = "catalog_sources_test"
ADMIN = UUID(int=71)
CONNECTION = UUID(int=72)
OTHER = UUID(int=73)


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> SourceStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = SourceStore(_config(), KINDS, pool)
    await built.setup()
    await built.setup()
    return built


async def test_sources_and_connections(store: SourceStore) -> None:
    prod = await store.create_source(
        SourceSpec(name="prod", description="Прод"), PgSourceKind.POSTGRES, ADMIN
    )
    assert prod.kind == PgSourceKind.POSTGRES
    assert prod.latest_version == 0

    dwh = await store.create_source(
        SourceSpec(name="dwh"), ChSourceKind.CLICKHOUSE, ADMIN
    )
    assert [s.name for s in await store.list_sources()] == ["dwh", "prod"]

    renamed = await store.update_source(
        prod.id, prod.spec().model_copy(update={"name": "prod2"})
    )
    assert renamed.name == "prod2"
    assert (await store.get_source(prod.id)).name == "prod2"

    bound = await store.bind_connection(
        prod.id, CONNECTION, PgSourceKind.POSTGRES, ADMIN
    )
    assert bound.connection_id == CONNECTION
    await store.bind_connection(prod.id, CONNECTION, PgSourceKind.POSTGRES, ADMIN)
    assert (await store.holder_of(CONNECTION)) == await store.get_source(prod.id)

    with pytest.raises(SourceKindMismatchError):
        await store.bind_connection(prod.id, OTHER, ChSourceKind.CLICKHOUSE, ADMIN)

    with pytest.raises(ConnectionAlreadyBoundError):
        await store.bind_connection(dwh.id, CONNECTION, ChSourceKind.CLICKHOUSE, ADMIN)

    assert [c.connection_id for c in await store.connections_of(prod.id)] == [
        CONNECTION
    ]
    assert await store.unbind_connection(prod.id, CONNECTION) is True
    assert await store.unbind_connection(prod.id, CONNECTION) is False

    assert await store.delete_source(dwh.id) is True
    with pytest.raises(SourceNotFoundError):
        await store.get_source(dwh.id)


async def test_postgres_version_round_trips(store: SourceStore) -> None:
    sample = PgSample()
    prod = await store.create_source(
        SourceSpec(name="prod"), PgSourceKind.POSTGRES, ADMIN
    )

    version = await store.write_version(
        prod.id,
        sample.snapshot(),
        VersionOrigin(taken_by=ADMIN, connection_id=CONNECTION, server_version="16.3"),
    )
    assert version.version == 1
    assert version.objects_total == 9
    assert version.server_version == "16.3"
    assert (await store.get_source(prod.id)).latest_version == 1

    stored = await store.snapshot_of(prod.id, 1)
    assert isinstance(stored, PgSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    assert await store.snapshot_of(prod.id, 0) == PgSnapshot.empty()
    with pytest.raises(SourceVersionNotFoundError):
        await store.snapshot_of(prod.id, 2)

    second = await store.write_version(
        prod.id, sample.next_version(), VersionOrigin(taken_by=ADMIN)
    )
    assert second.version == 2
    assert [v.version for v in await store.versions_of(prod.id)] == [1, 2]
    latest = await store.latest_snapshot(prod.id)
    assert _sorted(latest) == _sorted(sample.next_version())

    diff = await store.diff_of(prod.id, 1, 2)
    removed = ObjectRef(
        source_id=prod.id,
        kind=ObjectKind.RELATION,
        path=("prod", "public", "customers"),
    )
    assert diff.status_of(removed) is ChangeStatus.REMOVED
    orders = ObjectRef(
        source_id=prod.id, kind=ObjectKind.RELATION, path=("prod", "public", "orders")
    )
    assert diff.status_of(orders) is ChangeStatus.MODIFIED


async def test_clickhouse_version_round_trips(store: SourceStore) -> None:
    sample = ChSample()
    dwh = await store.create_source(
        SourceSpec(name="dwh"), ChSourceKind.CLICKHOUSE, ADMIN
    )

    await store.write_version(dwh.id, sample.snapshot(), VersionOrigin(taken_by=ADMIN))
    stored = await store.snapshot_of(dwh.id, 1)
    assert isinstance(stored, ChSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    tree = stored.children(dwh.id, ("dwh", "tables"))
    assert [node.label for node in tree] == ["events"]


async def test_snapshot_kind_must_match_source(store: SourceStore) -> None:
    dwh = await store.create_source(
        SourceSpec(name="dwh"), ChSourceKind.CLICKHOUSE, ADMIN
    )
    with pytest.raises(Exception, match="snapshot is postgres"):
        await store.write_version(
            dwh.id, PgSample().snapshot(), VersionOrigin(taken_by=ADMIN)
        )


def _sorted(snapshot: SourceSnapshot) -> dict[str, list[SourceRecord]]:
    """Записи по частям, отсортированные по ключу: порядок строк из базы не
    гарантирован, а содержимое должно совпасть целиком."""
    tables: dict[str, list[SourceRecord]] = {}
    for part in snapshot.parts():
        records = list(snapshot.records_of(part.name))
        records.sort(key=lambda record: record.key)
        tables[part.name] = records

    return tables
