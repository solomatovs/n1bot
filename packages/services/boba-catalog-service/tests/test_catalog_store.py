"""CatalogStore на реальном postgres: снимок из таблиц, черновики, гонка порций,
публикация, перебазирование, виды.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from psycopg import sql
from sample_ops import Sample

from boba.catalog import (
    AddDataset,
    AddLayer,
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    Dataset,
    EntityRef,
    Layer,
    OperationList,
    RemoveColumn,
    RemoveDataset,
    RemoveFlow,
    SetColumn,
    SetDataset,
    SetLayer,
)
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogStore,
    DraftAuthor,
    DraftClosedError,
    DraftConflictError,
    DraftNotFoundError,
    DraftStaleError,
    DraftStatus,
    NodePosition,
    ViewNotFoundError,
    ViewShare,
    ViewSpec,
)
from boba.db.postgres import AsyncPostgresPool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_test"
EDITOR = UUID(int=7)
OTHER = UUID(int=8)


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
    return built


@pytest.fixture
def sample() -> Sample:
    return Sample()


async def _published(store: CatalogStore, sample: Sample) -> CatalogSnapshot:
    """Черновик с образцом, опубликованный как версия 1."""
    draft = await store.create_draft("initial", EDITOR)
    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops())
    await store.publish(draft.id, _author(EDITOR))

    return await store.snapshot()


async def test_setup_is_idempotent_and_catalog_starts_empty(
    store: CatalogStore,
) -> None:
    await store.setup()

    assert await store.current_version() == 0
    assert await store.snapshot() == CatalogSnapshot.empty()
    assert list(await store.versions()) == []


async def test_publish_empty_draft_makes_version_without_changes(
    store: CatalogStore,
) -> None:
    draft = await store.create_draft("nothing", EDITOR)

    version = await store.publish(draft.id, _author(EDITOR))

    assert version.number == 1
    assert version.operations.root == ()
    assert await store.current_version() == 1
    assert await store.snapshot() == CatalogSnapshot.empty()

    closed = await store.get_draft(draft.id)
    assert closed.status is DraftStatus.PUBLISHED


async def test_full_cycle_tables_match_memory(
    store: CatalogStore, sample: Sample
) -> None:
    expected = sample.ops().apply(CatalogSnapshot.empty())

    draft = await store.create_draft("initial", EDITOR)
    assert draft.base_version == 0

    portion = await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops())
    assert portion.seq == 1

    state = await store.draft_state(draft.id)
    assert state.snapshot == expected
    assert state.seq == 1
    assert state.diff.status_of(EntityRef.of(sample.flow_orders)) is ChangeStatus.ADDED

    version = await store.publish(draft.id, _author(EDITOR))
    assert version.number == 1
    assert version.operations.model_dump(mode="json") == sample.ops().model_dump(
        mode="json"
    )

    assert await store.snapshot() == expected
    assert await store.snapshot_at(1) == expected
    assert await store.snapshot_at(0) == CatalogSnapshot.empty()


async def test_second_version_rewrites_tables_by_diff(
    store: CatalogStore, sample: Sample
) -> None:
    base = await _published(store, sample)

    renamed = sample.stg_orders.model_copy(update={"name": "orders_clean", "tags": ()})
    retyped = sample.raw_orders_amount.model_copy(update={"type": "decimal(18,2)"})
    ods = Layer(id=UUID(int=103), name="ods")
    ops = OperationList(
        root=(
            SetDataset(dataset=renamed),
            SetColumn(column=retyped),
            AddLayer(layer=ods),
            RemoveFlow(id=sample.flow_items.id),
            RemoveDataset(id=sample.raw_items.id),
        )
    )
    expected = ops.apply(base)

    draft = await store.create_draft("second", EDITOR)
    assert draft.base_version == 1
    await store.append_ops(draft.id, 0, _author(EDITOR), ops)
    version = await store.publish(draft.id, _author(EDITOR))

    assert version.number == 2
    assert await store.snapshot() == expected
    assert await store.snapshot_at(1) == base
    assert await store.snapshot_at(2) == expected


async def test_name_swap_inside_one_publish(
    store: CatalogStore, sample: Sample
) -> None:
    """Уникальность имён отложена до конца транзакции: обмен именами проходит."""
    base = await _published(store, sample)

    raw_as_stg = sample.raw.model_copy(update={"name": "stg"})
    stg_as_raw = sample.stg.model_copy(update={"name": "raw"})
    ops = OperationList(
        root=(
            SetLayer(layer=sample.raw.model_copy(update={"name": "tmp"})),
            SetLayer(layer=stg_as_raw),
            SetLayer(layer=raw_as_stg),
        )
    )

    draft = await store.create_draft("swap", EDITOR)
    await store.append_ops(draft.id, 0, _author(EDITOR), ops)
    await store.publish(draft.id, _author(EDITOR))

    published = await store.snapshot()
    assert published.layers[sample.raw.id].name == "stg"
    assert published.layers[sample.stg.id].name == "raw"
    assert published != base


async def test_column_reference_strings_round_trip_through_jsonb(
    store: CatalogStore, sample: Sample
) -> None:
    """Ссылки на колонки в load_values едут строками и возвращаются UUID."""
    await _published(store, sample)

    published = await store.snapshot()
    values = published.flows[sample.flow_orders.id].load.values

    assert values["key"] == sample.stg_orders_id.id
    assert values["hash_columns"] == (
        sample.raw_orders_id.id,
        sample.raw_orders_amount.id,
    )
    assert values["batch"] == 500


async def test_append_rejects_stale_seq_and_bad_ops(
    store: CatalogStore, sample: Sample
) -> None:
    draft = await store.create_draft("initial", EDITOR)
    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops())

    with pytest.raises(DraftConflictError) as conflict:
        await store.append_ops(draft.id, 0, _author(OTHER), OperationList(root=()))

    assert conflict.value.current_seq == 1
    assert conflict.value.expected_seq == 0

    duplicate = OperationList(root=(AddLayer(layer=sample.raw),))
    with pytest.raises(CatalogOpError) as failure:
        await store.append_ops(draft.id, 1, _author(EDITOR), duplicate)

    assert failure.value.index == 0
    assert "already exists" in failure.value.reason

    state = await store.draft_state(draft.id)
    assert state.seq == 1


async def test_two_authors_race_on_same_seq(
    store: CatalogStore, sample: Sample
) -> None:
    """Две порции с одним expected_seq параллельно: проходит ровно одна."""
    draft = await store.create_draft("race", EDITOR)

    human = OperationList(root=(AddLayer(layer=sample.raw),))
    model = OperationList(root=(AddLayer(layer=sample.stg),))

    outcomes = await asyncio.gather(
        store.append_ops(draft.id, 0, _author(EDITOR), human),
        store.append_ops(draft.id, 0, _author(OTHER), model),
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
    store: CatalogStore, sample: Sample
) -> None:
    await _published(store, sample)

    lagging = await store.create_draft("lagging", EDITOR)
    ods = Layer(id=UUID(int=103), name="ods")
    await store.append_ops(
        lagging.id, 0, _author(EDITOR), OperationList(root=(AddLayer(layer=ods),))
    )

    racing = await store.create_draft("racing", OTHER)
    dm = Layer(id=UUID(int=104), name="dm")
    await store.append_ops(
        racing.id, 0, _author(OTHER), OperationList(root=(AddLayer(layer=dm),))
    )
    await store.publish(racing.id, _author(OTHER))

    with pytest.raises(DraftStaleError) as stale:
        await store.publish(lagging.id, _author(EDITOR))

    assert stale.value.base_version == 1
    assert stale.value.current_version == 2

    result = await store.rebase(lagging.id, drop_conflicts=False)
    assert result.issues == ()
    assert result.draft.base_version == 2

    version = await store.publish(lagging.id, _author(EDITOR))
    assert version.number == 3

    published = await store.snapshot()
    assert {layer.name for layer in published.layers.values()} == {
        "raw",
        "stg",
        "ods",
        "dm",
    }


async def test_rebase_reports_conflicts_and_drops_them_on_request(
    store: CatalogStore, sample: Sample
) -> None:
    await _published(store, sample)

    lagging = await store.create_draft("lagging", EDITOR)
    touch_items = SetDataset(
        dataset=sample.raw_items.model_copy(update={"description": "touched"})
    )
    add_ods = AddLayer(layer=Layer(id=UUID(int=103), name="ods"))
    await store.append_ops(
        lagging.id, 0, _author(EDITOR), OperationList(root=(touch_items, add_ods))
    )

    remover = await store.create_draft("remover", OTHER)
    drop_items = OperationList(
        root=(
            RemoveFlow(id=sample.flow_items.id),
            RemoveDataset(id=sample.raw_items.id),
        )
    )
    await store.append_ops(remover.id, 0, _author(OTHER), drop_items)
    await store.publish(remover.id, _author(OTHER))

    reported = await store.rebase(lagging.id, drop_conflicts=False)
    assert reported.draft.base_version == 1
    assert len(reported.issues) == 1
    assert reported.issues[0].seq == 1
    assert reported.issues[0].index == 0
    assert "not found" in reported.issues[0].reason

    with pytest.raises(DraftStaleError):
        await store.publish(lagging.id, _author(EDITOR))

    dropped = await store.rebase(lagging.id, drop_conflicts=True)
    assert dropped.draft.base_version == 2
    assert len(dropped.issues) == 1

    portions = await store.draft_ops(lagging.id)
    assert portions[0].operations.root == (add_ods,)

    version = await store.publish(lagging.id, _author(EDITOR))
    assert version.number == 3
    assert "ods" in {layer.name for layer in (await store.snapshot()).layers.values()}


async def test_closed_draft_rejects_portions_and_publish(
    store: CatalogStore, sample: Sample
) -> None:
    draft = await store.create_draft("once", EDITOR)
    await store.publish(draft.id, _author(EDITOR))

    with pytest.raises(DraftClosedError):
        await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops())

    with pytest.raises(DraftClosedError):
        await store.publish(draft.id, _author(EDITOR))

    discarded = await store.create_draft("gone", EDITOR)
    await store.discard_draft(discarded.id)

    assert (await store.get_draft(discarded.id)).status is DraftStatus.DISCARDED
    assert [d.name for d in await store.list_drafts(DraftStatus.OPEN)] == []

    with pytest.raises(DraftNotFoundError):
        await store.get_draft(UUID(int=999))


async def test_draft_state_diff_against_base(
    store: CatalogStore, sample: Sample
) -> None:
    await _published(store, sample)

    draft = await store.create_draft("edits", EDITOR)
    ops = OperationList(
        root=(
            SetColumn(
                column=sample.raw_orders_amount.model_copy(update={"nullable": False})
            ),
            RemoveFlow(id=sample.flow_items.id),
            AddDataset(
                dataset=Dataset(id=UUID(int=113), layer_id=sample.stg.id, name="items")
            ),
        )
    )
    await store.append_ops(draft.id, 0, _author(EDITOR), ops)

    state = await store.draft_state(draft.id)

    assert (
        state.diff.status_of(EntityRef.of(sample.raw_orders_amount))
        is ChangeStatus.MODIFIED
    )
    assert state.diff.status_of(EntityRef.of(sample.flow_items)) is ChangeStatus.REMOVED
    assert (
        state.diff.status_of(EntityRef.of(sample.raw_orders)) is ChangeStatus.UNCHANGED
    )
    assert len(state.diff.entries) == 3
    assert sample.flow_items.id not in state.snapshot.flows
    assert (await store.snapshot()).flows[sample.flow_items.id] == sample.flow_items


async def test_portion_keeps_author_and_stale_base_snapshot(
    store: CatalogStore, sample: Sample
) -> None:
    """Порция проверяется против снимка базовой версии черновика, а не текущей."""
    await _published(store, sample)

    lagging = await store.create_draft("lagging", EDITOR)

    remover = await store.create_draft("remover", OTHER)
    await store.append_ops(
        remover.id,
        0,
        _author(OTHER),
        OperationList(
            root=(
                RemoveFlow(id=sample.flow_items.id),
                RemoveDataset(id=sample.raw_items.id),
            )
        ),
    )
    await store.publish(remover.id, _author(OTHER))

    llm = DraftAuthor(user_id=EDITOR, via=AuthorVia.LLM)
    touch = OperationList(
        root=(SetDataset(dataset=sample.raw_items.model_copy(update={"owner": "llm"})),)
    )
    portion = await store.append_ops(lagging.id, 0, llm, touch)

    assert portion.author == llm
    assert (await store.draft_ops(lagging.id))[0].author.via is AuthorVia.LLM

    state = await store.draft_state(lagging.id)
    assert state.snapshot.datasets[sample.raw_items.id].owner == "llm"


async def test_remove_column_referenced_by_flow_is_refused_in_draft(
    store: CatalogStore, sample: Sample
) -> None:
    await _published(store, sample)
    draft = await store.create_draft("edits", EDITOR)

    with pytest.raises(CatalogOpError) as failure:
        await store.append_ops(
            draft.id,
            0,
            _author(EDITOR),
            OperationList(root=(RemoveColumn(id=sample.raw_orders_id.id),)),
        )

    assert "referenced by 1 flow(s)" in failure.value.reason


async def test_views_layout_and_shares(store: CatalogStore, sample: Sample) -> None:
    await _published(store, sample)

    view = await store.create_view(
        EDITOR,
        ViewSpec(
            name="orders", dataset_ids=(sample.raw_orders.id, sample.stg_orders.id)
        ),
    )
    assert view.owner_id == EDITOR
    assert view.layer_ids == ()

    renamed = await store.update_view(
        view.id, ViewSpec(name="orders flow", layer_ids=(sample.stg.id,))
    )
    assert renamed.name == "orders flow"
    assert renamed.dataset_ids == ()
    assert renamed.layer_ids == (sample.stg.id,)

    layout = await store.put_layout(
        view.id,
        [
            NodePosition(dataset_id=sample.raw_orders.id, x=10.5, y=20),
            NodePosition(dataset_id=sample.stg_orders.id, x=300, y=20),
        ],
    )
    assert len(layout.positions) == 2
    assert (await store.layout_of(view.id)) == layout

    replaced = await store.put_layout(
        view.id, [NodePosition(dataset_id=sample.raw_orders.id, x=1, y=1)]
    )
    assert len(replaced.positions) == 1

    await store.share_view(view.id, ViewShare.role("analyst"))
    await store.share_view(view.id, ViewShare.user(OTHER))
    await store.share_view(view.id, ViewShare.user(OTHER))
    shares = await store.shares_of(view.id)
    assert [share.target for share in shares] == ["analyst", str(OTHER)]

    assert [v.id for v in await store.views_for(OTHER, [], everything=False)] == [
        view.id
    ]
    assert [
        v.id for v in await store.views_for(UUID(int=9), ["analyst"], everything=False)
    ] == [view.id]
    assert list(await store.views_for(UUID(int=9), ["nobody"], everything=False)) == []
    assert [v.id for v in await store.views_for(UUID(int=9), [], everything=True)] == [
        view.id
    ]

    assert await store.unshare_view(view.id, ViewShare.user(OTHER))
    assert not await store.unshare_view(view.id, ViewShare.user(OTHER))
    assert list(await store.views_for(OTHER, [], everything=False)) == []

    assert await store.delete_view(view.id)
    assert not await store.delete_view(view.id)

    with pytest.raises(ViewNotFoundError):
        await store.layout_of(view.id)
