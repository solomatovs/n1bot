"""Хранилище процессов на живом Postgres: процессы, черновики с порциями,
публикация таблицами, история версий, гонка авторов, устаревший черновик и
rebase с конфликтами, привязки к версиям снимков, ссылки на просмотр, узлы
над подключением."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    AcceptAll,
    AddGroup,
    AddNode,
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    EntityRef,
    Group,
    Node,
    ObjectKind,
    OperationList,
    Position,
    RemoveFlow,
    RemoveNode,
    SetNode,
    SnapshotResolver,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogStoreError,
    DraftAuthor,
    DraftClosedError,
    DraftConflictError,
    DraftStaleError,
    DraftStatus,
    ProcessNameTakenError,
    ProcessNotFoundError,
    ProcessSpec,
    ProcessStore,
    ShareNotFoundError,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot_sample import PgSample

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_test"
EDITOR = UUID(int=7)
OTHER = UUID(int=8)
CONNECTION = UUID(int=0x5001)


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


def _author(user_id: UUID) -> DraftAuthor:
    return DraftAuthor(user_id=user_id, via=AuthorVia.USER)


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ProcessStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = ProcessStore(_config(), pool)
    await built.setup()
    await built.setup()
    return built


@pytest.fixture
async def process_id(store: ProcessStore) -> UUID:
    process = await store.create_process(ProcessSpec(name="orders"), EDITOR)
    return process.id


@pytest.fixture
def sample() -> ProcessSample:
    return ProcessSample(CONNECTION)


@pytest.fixture
def resolver() -> SnapshotResolver:
    return SnapshotResolver({CONNECTION: PgSample().snapshot()})


async def _published(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> CatalogSnapshot:
    """Черновик с образцом, опубликованный как версия 1."""
    draft = await store.create_draft(process_id, "initial", EDITOR, {CONNECTION: 1})
    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)
    await store.publish(draft.id, _author(EDITOR))

    return await store.snapshot(process_id)


async def test_processes_are_named_uniquely_and_listed_with_counts(
    store: ProcessStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    orders = await store.create_process(
        ProcessSpec(name="orders", description="sales"), EDITOR
    )
    assert orders.owner_id == EDITOR
    assert orders.latest_version == 0
    assert orders.nodes == 0
    assert orders.open_drafts == 0
    assert await store.current_version(orders.id) == 0
    assert await store.snapshot(orders.id) == CatalogSnapshot.empty()
    assert list(await store.versions(orders.id)) == []

    with pytest.raises(ProcessNameTakenError):
        await store.create_process(ProcessSpec(name="orders"), EDITOR)

    other = await store.create_process(ProcessSpec(name="stock"), OTHER)
    with pytest.raises(ProcessNameTakenError):
        await store.update_process(other.id, ProcessSpec(name="orders"))

    renamed = await store.update_process(
        other.id, ProcessSpec(name="stock2", description="warehouse")
    )
    assert renamed.spec() == ProcessSpec(name="stock2", description="warehouse")

    await _published(store, orders.id, sample, resolver)
    await store.create_draft(orders.id, "wip", EDITOR, {CONNECTION: 1})
    listed = {process.name: process for process in await store.list_processes()}
    assert listed["orders"].nodes == len(sample.snapshot().nodes)
    assert listed["orders"].latest_version == 1
    assert listed["orders"].open_drafts == 1
    assert listed["stock2"].nodes == 0

    assert await store.delete_process(other.id) is True
    assert await store.delete_process(other.id) is False
    with pytest.raises(ProcessNotFoundError):
        await store.get_process(other.id)


async def test_two_processes_keep_their_groups_and_nodes_apart(
    store: ProcessStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    """Одинаковые группы и объекты в двух процессах не мешают друг другу."""
    first = await store.create_process(ProcessSpec(name="first"), EDITOR)
    second = await store.create_process(ProcessSpec(name="second"), EDITOR)
    await _published(store, first.id, sample, resolver)

    twin = ProcessSample(CONNECTION)
    twin.raw = twin.raw.model_copy(update={"id": UUID(int=0x8101)})
    twin.dm = twin.dm.model_copy(update={"id": UUID(int=0x8102)})
    ops = OperationList(
        root=(
            AddGroup(group=twin.raw),
            AddGroup(group=twin.dm),
            AddNode(
                node=sample.orders.model_copy(
                    update={"id": UUID(int=0x8201), "group_id": twin.raw.id}
                )
            ),
        )
    )
    draft = await store.create_draft(second.id, "d", EDITOR, {CONNECTION: 1})
    await store.append_ops(draft.id, 0, _author(EDITOR), ops, resolver)
    await store.publish(draft.id, _author(EDITOR))

    assert len((await store.snapshot(first.id)).nodes) == 4
    assert len((await store.snapshot(second.id)).nodes) == 1
    usage = await store.usage_of_connection(CONNECTION)
    assert {(entry.process_name, entry.nodes) for entry in usage} == {
        ("first", 4),
        ("second", 1),
    }
    assert await store.usage_of_connection(UUID(int=0x5999)) == []


async def test_full_cycle_tables_match_memory(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    expected = sample.ops().apply(CatalogSnapshot.empty(), resolver)

    draft = await store.create_draft(process_id, "initial", EDITOR, {CONNECTION: 1})
    assert draft.base_version == 0
    assert draft.process_id == process_id
    assert draft.pins == {CONNECTION: 1}

    portion = await store.append_ops(
        draft.id, 0, _author(EDITOR), sample.ops(), resolver
    )
    assert portion.seq == 1

    state = await store.draft_state(draft.id)
    assert state.snapshot == expected
    assert state.diff.status_of(EntityRef.of(sample.flow_orders)) is ChangeStatus.ADDED

    version = await store.publish(draft.id, _author(EDITOR))
    assert version.number == 1
    assert version.process_id == process_id
    assert version.pins == {CONNECTION: 1}
    assert version.operations.model_dump(mode="json") == sample.ops().model_dump(
        mode="json"
    )

    assert await store.snapshot(process_id) == expected
    assert await store.snapshot_at(process_id, 1) == expected
    assert await store.snapshot_at(process_id, 0) == CatalogSnapshot.empty()
    assert (await store.versions(process_id))[0].pins == {CONNECTION: 1}


async def test_second_version_rewrites_tables_by_diff(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    base = await _published(store, process_id, sample, resolver)

    renamed = sample.customers.model_copy(update={"alias": "buyers", "note": "vip"})
    ods = Group(id=UUID(int=0x7103), name="ods")
    moved = sample.orders.model_copy(
        update={"position": Position(x=15.5, y=-3), "group_id": None}
    )
    ops = OperationList(
        root=(
            SetNode(node=renamed),
            SetNode(node=moved),
            AddGroup(group=ods),
            RemoveFlow(id=sample.flow_customers.id),
        )
    )
    expected = ops.apply(base, resolver)

    draft = await store.create_draft(process_id, "second", EDITOR, {CONNECTION: 1})
    assert draft.base_version == 1
    await store.append_ops(draft.id, 0, _author(EDITOR), ops, resolver)
    version = await store.publish(draft.id, _author(EDITOR))

    assert version.number == 2
    assert await store.snapshot(process_id) == expected
    assert await store.snapshot_at(process_id, 1) == base
    assert await store.snapshot_at(process_id, 2) == expected
    stored = (await store.snapshot(process_id)).nodes[sample.orders.id]
    assert stored.position == Position(x=15.5, y=-3)
    assert stored.group_id is None


async def test_append_rejects_stale_seq_and_bad_ops(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    draft = await store.create_draft(process_id, "d", EDITOR, {CONNECTION: 1})
    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)

    with pytest.raises(DraftConflictError) as conflict:
        await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)

    assert conflict.value.current_seq == 1

    ghost = Node(
        id=UUID(int=0x7299),
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
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    """Две порции с одним expected_seq параллельно: проходит ровно одна."""
    draft = await store.create_draft(process_id, "race", EDITOR, {})

    human = OperationList(root=(AddGroup(group=sample.raw),))
    model = OperationList(root=(AddGroup(group=sample.dm),))

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
    assert len(state.snapshot.groups) == 1


async def test_stale_draft_refuses_publish_until_rebased(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    await _published(store, process_id, sample, resolver)

    lagging = await store.create_draft(process_id, "lagging", EDITOR, {CONNECTION: 1})
    ods = Group(id=UUID(int=0x7103), name="ods")
    await store.append_ops(
        lagging.id,
        0,
        _author(EDITOR),
        OperationList(root=(AddGroup(group=ods),)),
        resolver,
    )

    racing = await store.create_draft(process_id, "racing", OTHER, {CONNECTION: 1})
    stage = Group(id=UUID(int=0x7104), name="stage")
    await store.append_ops(
        racing.id,
        0,
        _author(OTHER),
        OperationList(root=(AddGroup(group=stage),)),
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

    published = await store.snapshot(process_id)
    assert {group.name for group in published.groups.values()} == {
        "raw",
        "dm",
        "ods",
        "stage",
    }


async def test_rebase_reports_conflicts_and_drops_them_on_request(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    await _published(store, process_id, sample, resolver)

    lagging = await store.create_draft(process_id, "lagging", EDITOR, {CONNECTION: 1})
    touch = SetNode(node=sample.customers.model_copy(update={"note": "touched"}))
    add_ods = AddGroup(group=Group(id=UUID(int=0x7103), name="ods"))
    await store.append_ops(
        lagging.id, 0, _author(EDITOR), OperationList(root=(touch, add_ods)), resolver
    )

    remover = await store.create_draft(process_id, "remover", OTHER, {CONNECTION: 1})
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
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    draft = await store.create_draft(process_id, "d", EDITOR, {CONNECTION: 1})
    moved = await store.set_pins(draft.id, {CONNECTION: 2})
    assert moved.pins == {CONNECTION: 2}
    assert (await store.get_draft(draft.id)).pins == {CONNECTION: 2}

    discarded = await store.discard_draft(draft.id)
    assert discarded.status is DraftStatus.DISCARDED

    with pytest.raises(DraftClosedError):
        await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)

    with pytest.raises(DraftClosedError):
        await store.publish(draft.id, _author(EDITOR))

    with pytest.raises(DraftClosedError):
        await store.set_pins(draft.id, {})


async def test_history_folds_without_snapshots(
    store: ProcessStore,
    process_id: UUID,
    sample: ProcessSample,
    resolver: SnapshotResolver,
) -> None:
    """Свёртка истории версий не ходит в снимки: AcceptAll."""
    await _published(store, process_id, sample, resolver)
    folded = await store.snapshot_at(process_id, 1)
    assert folded == sample.ops().apply(CatalogSnapshot.empty(), AcceptAll())


async def test_share_links_are_issued_and_revoked(
    store: ProcessStore, process_id: UUID
) -> None:
    share = await store.create_share(process_id, EDITOR)
    assert share.process_id == process_id
    assert share.revoked_at is None
    assert len(share.token) >= 20
    assert (await store.get_share(share.token)).token == share.token
    assert [entry.token for entry in await store.shares_of(process_id)] == [share.token]

    revoked = await store.revoke_share(share.token)
    assert revoked.revoked_at is not None
    assert await store.shares_of(process_id) == []
    with pytest.raises(ShareNotFoundError):
        await store.get_share(share.token)

    with pytest.raises(ShareNotFoundError):
        await store.revoke_share("no-such-token")


async def test_draft_without_a_process_publishes_into_a_new_process(
    store: ProcessStore, sample: ProcessSample, resolver: SnapshotResolver
) -> None:
    """Черновик нового процесса: над пустым снимком, без rebase, в списке
    автора; публикация создаёт процесс с именем черновика и версию 1, занятое
    имя — отказ, а переименование чинит."""
    draft = await store.create_draft(None, "refunds", EDITOR, {CONNECTION: 1})
    assert draft.process_id is None
    assert draft.base_version == 0
    assert (await store.draft_state(draft.id)).snapshot == CatalogSnapshot.empty()

    await store.append_ops(draft.id, 0, _author(EDITOR), sample.ops(), resolver)
    state = await store.draft_state(draft.id)
    assert len(state.snapshot.nodes) == len(sample.snapshot().nodes)
    rebased = await store.rebase(draft.id, drop_conflicts=False, resolver=resolver)
    assert rebased.draft.base_version == 0

    mine = await store.drafts_of_author(EDITOR)
    assert [entry.id for entry in mine] == [draft.id]
    assert await store.drafts_of_author(OTHER) == []
    assert [entry.id for entry in await store.open_drafts()] == [draft.id]

    await store.create_process(ProcessSpec(name="refunds"), OTHER)
    with pytest.raises(ProcessNameTakenError):
        await store.publish(draft.id, _author(EDITOR))

    still_open = await store.get_draft(draft.id)
    assert still_open.process_id is None
    assert still_open.status is DraftStatus.OPEN

    renamed = await store.rename_draft(draft.id, "refunds v2")
    assert renamed.name == "refunds v2"
    version = await store.publish(draft.id, _author(EDITOR))
    assert version.number == 1

    process = await store.get_process(version.process_id)
    assert process.name == "refunds v2"
    assert process.owner_id == EDITOR
    assert process.latest_version == 1
    assert await store.snapshot(process.id) == sample.snapshot()
    published = await store.get_draft(draft.id)
    assert published.process_id == process.id
    assert published.status is DraftStatus.PUBLISHED
    with pytest.raises(DraftClosedError):
        await store.rename_draft(draft.id, "late")


async def test_setup_refuses_a_table_of_another_layout(pool: AsyncPostgresPool) -> None:
    """Таблица nodes старого выпуска без process_id: setup не молчит до первого
    запроса, а отказывает с расхождением колонок и советом снести схему."""
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
        await conn.execute(sql.SQL("create schema {}").format(sql.Identifier(SCHEMA)))
        await conn.execute(
            sql.SQL(
                "create table {}.nodes (id uuid primary key, source_id uuid not null)"
            ).format(sql.Identifier(SCHEMA))
        )

    with pytest.raises(CatalogStoreError) as refused:
        await ProcessStore(_config(), pool).setup()

    text = str(refused.value)
    assert f"{SCHEMA}.nodes" in text
    assert "missing columns ['alias', 'connection_id'" in text
    assert "unexpected columns ['source_id']" in text
    assert "drop the schema" in text
