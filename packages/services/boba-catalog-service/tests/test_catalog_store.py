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
from boba.catalog.samples import PgSample, ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogStore,
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
