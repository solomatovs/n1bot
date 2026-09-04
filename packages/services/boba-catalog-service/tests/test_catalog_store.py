"""Хранилище процесса на живом Postgres: черновики с порциями, публикация
таблицами, история версий, гонка авторов, устаревший черновик и rebase с
конфликтами, привязки к версиям источников, виды с раскладкой и шарингом."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    AcceptAll,
    AddLayer,
    AddNode,
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    EntityRef,
    Layer,
    Node,
    ObjectKind,
    OperationList,
    RemoveFlow,
    RemoveNode,
    SetNode,
    SnapshotResolver,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogStore,
    CatalogStoreError,
    DraftAuthor,
    DraftClosedError,
    DraftConflictError,
    DraftStaleError,
    DraftStatus,
    NodePosition,
    ViewShare,
    ViewSpec,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot_sample import PgSample

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_test"
EDITOR = UUID(int=7)
OTHER = UUID(int=8)
SOURCE = UUID(int=0x5001)


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


def _author(user_id: UUID) -> DraftAuthor:
    return DraftAuthor(user_id=user_id, via=AuthorVia.USER)


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> CatalogStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = CatalogStore(_config(), pool)
    await built.setup()
    await built.setup()
    return built


@pytest.fixture
def sample() -> ProcessSample:
    return ProcessSample(SOURCE)


@pytest.fixture
def resolver() -> SnapshotResolver:
    return SnapshotResolver({SOURCE: PgSample().snapshot()})


async def _published(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> CatalogSnapshot:
    """Черновик с образцом, опубликованный как версия 1."""
    draft = await store.create_draft("initial", EDITOR, {SOURCE: 1})
    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)
    await store.publish(draft.id, _author(EDITOR))

    return await store.snapshot()


LEGACY_DDL = """
create table {s}.layers (id uuid primary key, name text not null,
    created_at timestamptz not null default now());
create table {s}.datasets (id uuid primary key, layer_id uuid not null
    references {s}.layers (id), name text not null, source text not null default '',
    description text not null default '', tags text[] not null default '{{}}',
    owner text not null default '');
create table {s}.columns (id uuid primary key, dataset_id uuid not null
    references {s}.datasets (id), name text not null, type text not null,
    nullable boolean not null, is_key boolean not null, position integer not null,
    description text not null default '');
create table {s}.load_kinds (id uuid primary key, name text not null,
    description text not null default '', fields jsonb not null default '[]');
create table {s}.flows (id uuid primary key,
    from_dataset_id uuid not null references {s}.datasets (id),
    to_dataset_id uuid not null references {s}.datasets (id),
    load_kind_id uuid not null references {s}.load_kinds (id),
    load_values jsonb not null default '{{}}', description text not null default '');
create table {s}.versions (number integer primary key, operations jsonb not null,
    author jsonb not null, published_at timestamptz not null default now());
create table {s}.drafts (id uuid primary key, name text not null,
    base_version integer not null, status text not null, created_by uuid not null,
    created_at timestamptz not null default now());
create table {s}.draft_ops (draft_id uuid not null references {s}.drafts (id),
    seq integer not null, author jsonb not null, operations jsonb not null,
    created_at timestamptz not null default now(), primary key (draft_id, seq));
create table {s}.views (id uuid primary key, name text not null,
    owner_id uuid not null, dataset_ids uuid[] not null default '{{}}',
    layer_ids uuid[] not null default '{{}}',
    created_at timestamptz not null default now());
create table {s}.view_layout (view_id uuid not null references {s}.views (id),
    dataset_id uuid not null, x double precision not null, y double precision not null,
    primary key (view_id, dataset_id));
create table {s}.view_shares (view_id uuid not null references {s}.views (id),
    target_kind text not null, target text not null, mode text not null,
    primary key (view_id, target_kind, target));
"""


async def _legacy_schema(pool: AsyncPostgresPool) -> None:
    """Схема первой очереди как в dev-базе: наборы, колонки, потоки по наборам,
    версии без привязок, виды по наборам."""
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
        await conn.execute(sql.SQL("create schema {}").format(sql.Identifier(SCHEMA)))
        for statement in LEGACY_DDL.format(s=SCHEMA).split(";"):
            if statement.strip() == "":
                continue

            await conn.execute(statement, prepare=False)


async def _columns_of(pool: AsyncPostgresPool, table: str) -> set[str]:
    async with pool.connection() as conn:
        rows = await conn.execute(
            "select column_name from information_schema.columns "
            "where table_schema = %s and table_name = %s",
            (SCHEMA, table),
        )
        return {row[0] for row in await rows.fetchall()}


async def test_setup_migrates_the_legacy_layout_without_dropping_anything(
    pool: AsyncPostgresPool,
) -> None:
    """Схема первой очереди переводится на месте: слои получают позицию и
    описание, версии и черновики — привязки, потоки и виды — узловые колонки;
    таблицы наборов остаются, повтор setup безвреден."""
    await _legacy_schema(pool)
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("insert into {} (id, name) values (%s, 'raw'), (%s, 'dm')").format(
                sql.Identifier(SCHEMA, "layers")
            ),
            (UUID(int=1), UUID(int=2)),
        )

    store = CatalogStore(_config(), pool)
    await store.setup()
    await store.setup()

    assert {"position", "description"} <= await _columns_of(pool, "layers")
    assert "pins" in await _columns_of(pool, "versions")
    assert "pins" in await _columns_of(pool, "drafts")
    flows = await _columns_of(pool, "flows")
    assert {"from_node_id", "to_node_id"} <= flows
    assert "from_dataset_id" not in flows
    assert "node_ids" in await _columns_of(pool, "views")
    assert "node_id" in await _columns_of(pool, "view_layout")
    assert await _columns_of(pool, "datasets") != set()
    assert await _columns_of(pool, "nodes") != set()
    assert [layer.name for layer in (await store.snapshot()).layers.values()] == [
        "dm",
        "raw",
    ]


async def test_setup_refuses_to_migrate_rows_that_reference_datasets(
    pool: AsyncPostgresPool,
) -> None:
    """Поток по наборам перенести нельзя: старт останавливается с текстом, в
    какой таблице сколько строк и что с ними делать; ничего не удалено."""
    await _legacy_schema(pool)
    ids = {
        "l": UUID(int=1),
        "a": UUID(int=2),
        "b": UUID(int=3),
        "k": UUID(int=4),
        "f": UUID(int=5),
    }
    inserts = (
        sql.SQL("insert into {} (id, name) values (%(l)s, 'raw')").format(
            sql.Identifier(SCHEMA, "layers")
        ),
        sql.SQL(
            "insert into {} (id, layer_id, name) "
            "values (%(a)s, %(l)s, 'orders'), (%(b)s, %(l)s, 'sales')"
        ).format(sql.Identifier(SCHEMA, "datasets")),
        sql.SQL("insert into {} (id, name) values (%(k)s, 'full')").format(
            sql.Identifier(SCHEMA, "load_kinds")
        ),
        sql.SQL(
            "insert into {} (id, from_dataset_id, to_dataset_id, load_kind_id) "
            "values (%(f)s, %(a)s, %(b)s, %(k)s)"
        ).format(sql.Identifier(SCHEMA, "flows")),
    )
    async with pool.connection() as conn:
        for statement in inserts:
            await conn.execute(statement, ids)

    store = CatalogStore(_config(), pool)
    with pytest.raises(CatalogStoreError) as refused:
        await store.setup()

    text = str(refused.value)
    assert f"{SCHEMA}.flows has 1 row(s) with the legacy column from_dataset_id" in text
    assert "move or delete these rows by hand" in text
    assert "from_dataset_id" in await _columns_of(pool, "flows")
    async with pool.connection() as conn:
        rows = await conn.execute(
            sql.SQL("select count(*) from {}").format(sql.Identifier(SCHEMA, "flows"))
        )
        assert (await rows.fetchone()) == (1,)


async def test_catalog_starts_empty(store: CatalogStore) -> None:
    assert await store.current_version() == 0
    assert await store.snapshot() == CatalogSnapshot.empty()
    assert list(await store.versions()) == []


async def test_full_cycle_tables_match_memory(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    expected = sample.ops().apply(CatalogSnapshot.empty(), resolver)

    draft = await store.create_draft("initial", EDITOR, {SOURCE: 1})
    assert draft.base_version == 0
    assert draft.pins == {SOURCE: 1}

    portion = await store.append_ops(
        draft.id, 0, _author(EDITOR), sample.ops(), resolver
    )
    assert portion.seq == 1

    state = await store.draft_state(draft.id)
    assert state.snapshot == expected
    assert state.diff.status_of(EntityRef.of(sample.flow_orders)) is ChangeStatus.ADDED

    version = await store.publish(draft.id, _author(EDITOR))
    assert version.number == 1
    assert version.pins == {SOURCE: 1}
    assert version.operations.model_dump(mode="json") == sample.ops().model_dump(
        mode="json"
    )

    assert await store.snapshot() == expected
    assert await store.snapshot_at(1) == expected
    assert await store.snapshot_at(0) == CatalogSnapshot.empty()
    assert (await store.versions())[0].pins == {SOURCE: 1}


async def test_second_version_rewrites_tables_by_diff(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    base = await _published(store, sample, resolver)

    renamed = sample.customers.model_copy(update={"alias": "buyers", "note": "vip"})
    ods = Layer(id=UUID(int=0x7103), name="ods", position=2)
    ops = OperationList(
        root=(
            SetNode(node=renamed),
            AddLayer(layer=ods),
            RemoveFlow(id=sample.flow_customers.id),
        )
    )
    expected = ops.apply(base, resolver)

    draft = await store.create_draft("second", EDITOR, {SOURCE: 1})
    assert draft.base_version == 1
    await store.append_ops(draft.id, 0, _author(EDITOR), ops, resolver)
    version = await store.publish(draft.id, _author(EDITOR))

    assert version.number == 2
    assert await store.snapshot() == expected
    assert await store.snapshot_at(1) == base
    assert await store.snapshot_at(2) == expected


async def test_append_rejects_stale_seq_and_bad_ops(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    draft = await store.create_draft("d", EDITOR, {SOURCE: 1})
    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)

    with pytest.raises(DraftConflictError) as conflict:
        await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)

    assert conflict.value.current_seq == 1

    ghost = Node(
        id=UUID(int=0x7299),
        layer_id=sample.raw.id,
        ref=sample.ref(ObjectKind.RELATION, ("prod", "public", "ghost")),
    )
    with pytest.raises(CatalogOpError) as rejected:
        await store.append_ops(
            draft.id,
            1,
            _author(EDITOR),
            OperationList(root=(AddNode(node=ghost),)),
            resolver,
        )

    assert "missing object" in rejected.value.reason
    assert (await store.draft_state(draft.id)).seq == 1


async def test_two_authors_race_on_same_seq(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    """Две порции с одним expected_seq параллельно: проходит ровно одна."""
    draft = await store.create_draft("race", EDITOR, {})

    human = OperationList(root=(AddLayer(layer=sample.raw),))
    model = OperationList(root=(AddLayer(layer=sample.dm),))

    outcomes = await asyncio.gather(
        store.append_ops(draft.id, 0, _author(EDITOR), human, resolver),
        store.append_ops(draft.id, 0, _author(OTHER), model, resolver),
        return_exceptions=True,
    )

    conflicts: list[DraftConflictError] = []
    accepted: list[UUID] = []
    for outcome in outcomes:
        if isinstance(outcome, DraftConflictError):
            conflicts.append(outcome)
            continue

        if isinstance(outcome, BaseException):
            raise outcome

        accepted.append(outcome.author.user_id)

    assert len(accepted) == 1
    assert len(conflicts) == 1
    assert conflicts[0].current_seq == 1

    state = await store.draft_state(draft.id)
    assert state.seq == 1
    assert len(state.snapshot.layers) == 1


async def test_stale_draft_refuses_publish_until_rebased(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    await _published(store, sample, resolver)

    lagging = await store.create_draft("lagging", EDITOR, {SOURCE: 1})
    ods = Layer(id=UUID(int=0x7103), name="ods", position=2)
    await store.append_ops(
        lagging.id,
        0,
        _author(EDITOR),
        OperationList(root=(AddLayer(layer=ods),)),
        resolver,
    )

    racing = await store.create_draft("racing", OTHER, {SOURCE: 1})
    stage = Layer(id=UUID(int=0x7104), name="stage", position=3)
    await store.append_ops(
        racing.id,
        0,
        _author(OTHER),
        OperationList(root=(AddLayer(layer=stage),)),
        resolver,
    )
    await store.publish(racing.id, _author(OTHER))

    with pytest.raises(DraftStaleError) as stale:
        await store.publish(lagging.id, _author(EDITOR))

    assert stale.value.base_version == 1
    assert stale.value.current_version == 2

    result = await store.rebase(lagging.id, drop_conflicts=False, resolver=resolver)
    assert result.issues == ()
    assert result.draft.base_version == 2

    version = await store.publish(lagging.id, _author(EDITOR))
    assert version.number == 3

    published = await store.snapshot()
    assert {layer.name for layer in published.layers.values()} == {
        "raw",
        "dm",
        "ods",
        "stage",
    }


async def test_rebase_reports_conflicts_and_drops_them_on_request(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    await _published(store, sample, resolver)

    lagging = await store.create_draft("lagging", EDITOR, {SOURCE: 1})
    touch = SetNode(node=sample.customers.model_copy(update={"note": "touched"}))
    add_ods = AddLayer(layer=Layer(id=UUID(int=0x7103), name="ods", position=2))
    await store.append_ops(
        lagging.id, 0, _author(EDITOR), OperationList(root=(touch, add_ods)), resolver
    )

    remover = await store.create_draft("remover", OTHER, {SOURCE: 1})
    drop = OperationList(
        root=(
            RemoveFlow(id=sample.flow_customers.id),
            RemoveNode(id=sample.customers.id),
        )
    )
    await store.append_ops(remover.id, 0, _author(OTHER), drop, resolver)
    await store.publish(remover.id, _author(OTHER))

    reported = await store.rebase(lagging.id, drop_conflicts=False, resolver=resolver)
    assert reported.draft.base_version == 1
    assert len(reported.issues) == 1
    assert reported.issues[0].seq == 1
    assert reported.issues[0].index == 0
    assert "not found" in reported.issues[0].reason

    dropped = await store.rebase(lagging.id, drop_conflicts=True, resolver=resolver)
    assert dropped.draft.base_version == 2
    portions = await store.draft_ops(lagging.id)
    assert portions[0].operations.root == (add_ods,)

    version = await store.publish(lagging.id, _author(EDITOR))
    assert version.number == 3


async def test_closed_draft_rejects_portions_and_pins_can_move(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    draft = await store.create_draft("d", EDITOR, {SOURCE: 1})
    moved = await store.set_pins(draft.id, {SOURCE: 2})
    assert moved.pins == {SOURCE: 2}
    assert (await store.get_draft(draft.id)).pins == {SOURCE: 2}

    discarded = await store.discard_draft(draft.id)
    assert discarded.status is DraftStatus.DISCARDED

    with pytest.raises(DraftClosedError):
        await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)

    with pytest.raises(DraftClosedError):
        await store.publish(draft.id, _author(EDITOR))

    with pytest.raises(DraftClosedError):
        await store.set_pins(draft.id, {})


async def test_history_folds_without_sources(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    """Свёртка истории версий не ходит в источники: AcceptAll."""
    await _published(store, sample, resolver)
    folded = await store.snapshot_at(1)
    assert folded == sample.ops().apply(CatalogSnapshot.empty(), AcceptAll())


async def test_views_layout_and_shares(
    store: CatalogStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    await _published(store, sample, resolver)

    view = await store.create_view(
        EDITOR, ViewSpec(name="orders", node_ids=(sample.orders.id,))
    )
    assert view.node_ids == (sample.orders.id,)
    assert (await store.get_view(view.id)).name == "orders"

    updated = await store.update_view(
        view.id, ViewSpec(name="orders2", layer_ids=(sample.raw.id,))
    )
    assert updated.layer_ids == (sample.raw.id,)
    assert updated.node_ids == ()

    layout = await store.put_layout(
        view.id, [NodePosition(node_id=sample.orders.id, x=1.5, y=2)]
    )
    assert layout.positions[0].node_id == sample.orders.id
    assert (await store.layout_of(view.id)).positions == layout.positions

    await store.share_view(view.id, ViewShare.role("analyst"))
    await store.share_view(view.id, ViewShare.user(OTHER))
    shares = await store.shares_of(view.id)
    assert {share.target for share in shares} == {"analyst", str(OTHER)}
    assert [v.id for v in await store.views_for(OTHER, [], everything=False)] == [
        view.id
    ]
    assert await store.unshare_view(view.id, ViewShare.role("analyst")) is True
    assert await store.delete_view(view.id) is True
    assert await store.views_for(EDITOR, [], everything=True) == []
