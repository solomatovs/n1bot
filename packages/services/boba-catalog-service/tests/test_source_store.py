"""Хранилище источников на живом Postgres: источники и привязки, запись версии
целиком и чтение обратно байт-в-байт, версии и diff, черновики ручного
источника с порциями, конфликтом, публикацией и устареванием."""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    AddObject,
    ChangeStatus,
    ChSnapshot,
    ManualColumn,
    ManualObject,
    ObjectKind,
    ObjectRef,
    PgSnapshot,
    RemoveObject,
    SourceKind,
    SourceOperationList,
    SourceOpError,
)
from boba.catalog.samples import ChSample, PgSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    DraftAuthor,
    DraftConflictError,
    DraftStaleError,
    DraftStatus,
    SourceNotFoundError,
    SourceNotManualError,
    SourceSpec,
    SourceStore,
    SourceVersionNotFoundError,
    VersionOrigin,
)
from boba.db.postgres import AsyncPostgresPool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_sources_test"
ADMIN = UUID(int=71)
CONNECTION = UUID(int=72)


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


def _author() -> DraftAuthor:
    return DraftAuthor(user_id=ADMIN, via=AuthorVia.USER)


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> SourceStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = SourceStore(_config(), pool)
    await built.setup()
    await built.setup()
    return built


async def test_sources_and_connections(store: SourceStore) -> None:
    prod = await store.create_source(
        SourceSpec(kind=SourceKind.POSTGRES, name="prod", description="Прод"), ADMIN
    )
    assert prod.kind is SourceKind.POSTGRES
    assert prod.latest_version == 0
    assert prod.manual is False

    dwh = await store.create_source(
        SourceSpec(kind=SourceKind.CLICKHOUSE, name="dwh"), ADMIN
    )
    assert [s.name for s in await store.list_sources()] == ["dwh", "prod"]

    renamed = await store.update_source(
        prod.id, prod.spec().model_copy(update={"name": "prod2"})
    )
    assert renamed.name == "prod2"
    assert (await store.get_source(prod.id)).name == "prod2"

    bound = await store.bind_connection(prod.id, CONNECTION, ADMIN)
    assert bound.connection_id == CONNECTION
    await store.bind_connection(prod.id, CONNECTION, ADMIN)
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
        SourceSpec(kind=SourceKind.POSTGRES, name="prod"), ADMIN
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
        SourceSpec(kind=SourceKind.CLICKHOUSE, name="dwh"), ADMIN
    )

    await store.write_version(dwh.id, sample.snapshot(), VersionOrigin(taken_by=ADMIN))
    stored = await store.snapshot_of(dwh.id, 1)
    assert isinstance(stored, ChSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    tree = stored.children(dwh.id, ("dwh", "tables"))
    assert [node.label for node in tree] == ["events"]


async def test_snapshot_kind_must_match_source(store: SourceStore) -> None:
    dwh = await store.create_source(
        SourceSpec(kind=SourceKind.CLICKHOUSE, name="dwh"), ADMIN
    )
    with pytest.raises(Exception, match="snapshot is postgres"):
        await store.write_version(
            dwh.id, PgSample().snapshot(), VersionOrigin(taken_by=ADMIN)
        )


async def test_manual_source_drafts(store: SourceStore) -> None:
    prod = await store.create_source(
        SourceSpec(kind=SourceKind.POSTGRES, name="prod"), ADMIN
    )
    with pytest.raises(SourceNotManualError):
        await store.create_draft(prod.id, "nope", ADMIN)

    planned = await store.create_source(
        SourceSpec(kind=SourceKind.POSTGRES, name="planned", manual=True), ADMIN
    )
    draft = await store.create_draft(planned.id, "first objects", ADMIN)
    assert draft.base_version == 0
    assert draft.status is DraftStatus.OPEN
    assert [d.id for d in await store.open_drafts(planned.id)] == [draft.id]

    sales = ManualObject(
        path=("planned", "dm", "sales"),
        comment="Витрина",
        columns=(ManualColumn(name="day", type="date", nullable=False),),
    )
    state = await store.append_ops(
        draft.id, 0, SourceOperationList(root=(AddObject(object=sales),)), _author()
    )
    assert state.seq == 1
    assert isinstance(state.snapshot, PgSnapshot)
    assert state.snapshot.relation(("planned", "dm", "sales")) is not None
    added = ObjectRef(
        source_id=planned.id, kind=ObjectKind.RELATION, path=("planned", "dm", "sales")
    )
    assert state.diff.status_of(added) is ChangeStatus.ADDED

    with pytest.raises(DraftConflictError) as conflict:
        await store.append_ops(
            draft.id, 0, SourceOperationList(root=(AddObject(object=sales),)), _author()
        )

    assert conflict.value.current_seq == 1

    with pytest.raises(SourceOpError):
        await store.append_ops(
            draft.id, 1, SourceOperationList(root=(AddObject(object=sales),)), _author()
        )

    reread = await store.draft_state(draft.id)
    assert reread.seq == 1
    assert reread.snapshot == state.snapshot

    version = await store.publish_draft(draft.id, _author())
    assert version.version == 1
    assert version.sync_id is None
    assert (await store.get_draft(draft.id)).status is DraftStatus.PUBLISHED
    published = await store.snapshot_of(planned.id, 1)
    assert isinstance(published, PgSnapshot)
    assert [r.name for r in published.relations] == ["sales"]

    stale = await store.create_draft(planned.id, "stale", ADMIN)
    await store.write_version(planned.id, published, VersionOrigin(taken_by=ADMIN))
    await store.append_ops(
        stale.id,
        0,
        SourceOperationList(root=(RemoveObject(path=("planned", "dm", "sales")),)),
        _author(),
    )
    with pytest.raises(DraftStaleError) as error:
        await store.publish_draft(stale.id, _author())

    assert error.value.current_version == 2

    discarded = await store.discard_draft(stale.id)
    assert discarded.status is DraftStatus.DISCARDED
    assert await store.open_drafts(planned.id) == []


def _sorted(snapshot: PgSnapshot | ChSnapshot) -> dict[str, list[object]]:
    """Записи по таблицам, отсортированные по ключу: порядок строк из базы не
    гарантирован, а содержимое должно совпасть целиком."""
    tables: dict[str, list[object]] = {}
    for field in type(snapshot).model_fields:
        if field == "kind":
            continue

        records = list(getattr(snapshot, field))
        records.sort(key=lambda record: record.key)
        tables[field] = records

    return tables
