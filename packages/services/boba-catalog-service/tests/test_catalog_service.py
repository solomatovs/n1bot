"""Сервис каталога на живом Postgres и шине в памяти: права по ролям,
черновики процесса над источником с привязками версий, публикация, rebase,
устаревание после новой версии источника и поднятие привязок, виды по
шарингу, источники и события шины."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    AddObject,
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    ManualColumn,
    ManualObject,
    ObjectKind,
    OperationList,
    RemoveFlow,
    RemoveNode,
    RetargetNode,
    SnapshotResolver,
    SourceKind,
    SourceOperationList,
    StaleReason,
)
from boba.catalog.samples import PgSample, ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogRefusalError,
    CatalogRefusalKind,
    CatalogService,
    CatalogStore,
    NodePosition,
    SourceSpec,
    SourceStore,
    ViewShare,
    ViewSpec,
)
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope, Subject
from boba.messaging import CatalogChanged, Envelope, MemoryMessageBus

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_service_test"


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


def _subject(user_id: UUID, *roles: str) -> Subject:
    return Subject.of_user(user_id, f"user-{user_id.int}", roles, "test")


EDITOR = _subject(UUID(int=1), "editor")
VIEWER = _subject(UUID(int=2), "viewer")
ANALYST = _subject(UUID(int=3), "analyst")
STRANGER = _subject(UUID(int=4))


@pytest.fixture
async def service(pool: AsyncPostgresPool) -> CatalogService:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    store = CatalogStore(_config(), pool)
    await store.setup()
    sources = SourceStore(_config(), pool)
    await sources.setup()
    return CatalogService(store, sources, _config(), MemoryMessageBus("test:0"))


@pytest.fixture
async def process(service: CatalogService) -> ProcessSample:
    """Источник prod с версией 1 из образца; процесс ссылается на него."""
    source = await service.create_source(
        EDITOR, SourceSpec(kind=SourceKind.POSTGRES, name="prod")
    )
    await service.write_source_version(EDITOR, source.id, PgSample().snapshot())
    return ProcessSample(source.id)


def _bus_of(service: CatalogService) -> MemoryMessageBus:
    bus = service.bus
    if not isinstance(bus, MemoryMessageBus):
        raise AssertionError("test service must run on the memory bus")

    return bus


class Collector:
    """Подписчик области пользователя: копит сообщения CatalogChanged."""

    def __init__(self) -> None:
        self.seen: list[CatalogChanged] = []

    async def __call__(self, envelope: Envelope) -> None:
        if not isinstance(envelope.message, CatalogChanged):
            return

        self.seen.append(envelope.message)


def _listen(
    service: CatalogService, subject: Subject
) -> tuple[Collector, Callable[[], None]]:
    collector = Collector()
    leave = _bus_of(service).subscribe(Scope.user(subject.user_id), collector)
    return collector, leave


async def test_roles_gate_reading_and_editing(
    service: CatalogService, process: ProcessSample
) -> None:
    with pytest.raises(CatalogRefusalError) as refused:
        await service.snapshot(STRANGER)

    assert refused.value.refusal is CatalogRefusalKind.VIEW_FORBIDDEN

    with pytest.raises(CatalogRefusalError) as refused:
        await service.create_draft(VIEWER, "no")

    assert refused.value.refusal is CatalogRefusalKind.EDIT_FORBIDDEN

    draft = await service.create_draft(EDITOR, "initial")
    assert draft.pins == {process.source_id: 1}

    state = await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.LLM)
    assert state.seq == 1
    resolver = SnapshotResolver({process.source_id: PgSample().snapshot()})
    assert state.snapshot == process.ops().apply(CatalogSnapshot.empty(), resolver)

    version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    assert version.number == 1
    assert version.pins == {process.source_id: 1}

    assert await service.snapshot(VIEWER) == state.snapshot
    assert [v.number for v in await service.versions(VIEWER)] == [1]


async def test_portions_are_checked_against_pinned_sources(
    service: CatalogService, process: ProcessSample
) -> None:
    draft = await service.create_draft(EDITOR, "checked")
    ghost = process.ref(ObjectKind.RELATION, ("prod", "public", "ghost"))
    with pytest.raises(CatalogOpError) as rejected:
        await service.append_ops(
            EDITOR,
            draft.id,
            0,
            OperationList(
                root=(
                    *process.ops().root,
                    RetargetNode(id=process.orders.id, ref=ghost),
                )
            ),
            AuthorVia.USER,
        )

    assert "missing object" in rejected.value.reason


async def test_new_source_version_marks_staleness_and_pins_can_bump(
    service: CatalogService, process: ProcessSample
) -> None:
    draft = await service.create_draft(EDITOR, "initial")
    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)
    assert (await service.staleness(VIEWER)).entries == ()

    await service.write_source_version(
        EDITOR, process.source_id, PgSample().next_version()
    )

    stale = await service.staleness(VIEWER)
    reasons = {(s.target.id, s.reason) for s in stale.entries}
    assert (process.customers.id, StaleReason.OBJECT_REMOVED) in reasons
    assert (process.flow_orders.id, StaleReason.COLUMN_CHANGED) in reasons

    context = await service.context(VIEWER)
    assert context.pins == {process.source_id: 1}
    assert [c.name for c in context.columns[process.orders.id]] == [
        "id",
        "amount",
        "created_at",
    ]
    assert [c.key for c in context.columns[process.orders.id]] == [True, False, True]
    assert context.columns[process.load_orders.id] == ()
    assert {s.reason for s in context.stale.entries} == {
        s.reason for s in stale.entries
    }

    lagging = await service.create_draft(EDITOR, "lagging")
    assert lagging.pins == {process.source_id: 2}
    assert (await service.draft_staleness(VIEWER, lagging.id)).entries == ()

    bump = await service.bump_pins(EDITOR, lagging.id)
    assert bump.draft.pins == {process.source_id: 2}
    assert any("customers" in violation for violation in bump.violations)


async def test_deleted_source_in_pins_does_not_break_the_context(
    service: CatalogService, process: ProcessSample
) -> None:
    """Источник удалён после публикации: привязка на него пропускается,
    контекст и устаревание считаются по оставшимся источникам."""
    draft = await service.create_draft(EDITOR, "initial")
    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)

    cleanup = await service.create_draft(EDITOR, "cleanup")
    ops = OperationList(
        root=(
            RemoveFlow(id=process.flow_orders.id),
            RemoveFlow(id=process.flow_customers.id),
            RemoveNode(id=process.orders.id),
            RemoveNode(id=process.customers.id),
            RemoveNode(id=process.v_orders.id),
            RemoveNode(id=process.load_orders.id),
        )
    )
    await service.append_ops(EDITOR, cleanup.id, 0, ops, AuthorVia.USER)
    await service.publish(EDITOR, cleanup.id, AuthorVia.USER)
    await service.delete_source(EDITOR, process.source_id)

    context = await service.context(VIEWER)
    assert context.pins == {process.source_id: 1}
    assert context.columns == {}
    assert context.stale.entries == ()
    assert (await service.create_draft(EDITOR, "after")).pins == {}


async def test_view_access_by_share(
    service: CatalogService, process: ProcessSample
) -> None:
    view = await service.create_view(
        EDITOR, ViewSpec(name="orders", node_ids=(process.orders.id,))
    )

    with pytest.raises(CatalogRefusalError):
        await service.view(ANALYST, view.id)

    with pytest.raises(CatalogRefusalError):
        await service.share_view(VIEWER, view.id, ViewShare.role("analyst"))

    await service.share_view(EDITOR, view.id, ViewShare.role("analyst"))
    await service.share_view(EDITOR, view.id, ViewShare.user(STRANGER.user_id))

    assert (await service.view(ANALYST, view.id)).id == view.id
    assert (await service.view(STRANGER, view.id)).id == view.id
    assert [v.id for v in await service.views(ANALYST)] == [view.id]

    with pytest.raises(CatalogRefusalError) as refused:
        await service.update_view(ANALYST, view.id, ViewSpec(name="mine"))

    assert refused.value.refusal is CatalogRefusalKind.EDIT_FORBIDDEN

    assert await service.unshare_view(EDITOR, view.id, ViewShare.role("analyst"))
    with pytest.raises(CatalogRefusalError):
        await service.view(ANALYST, view.id)


async def test_view_state_slices_the_process_for_a_shared_stranger(
    service: CatalogService, process: ProcessSample
) -> None:
    draft = await service.create_draft(EDITOR, "initial")
    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.LLM)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)

    view = await service.create_view(
        EDITOR, ViewSpec(name="raw", layer_ids=(process.raw.id,))
    )
    await service.put_layout(
        EDITOR, view.id, [NodePosition(node_id=process.orders.id, x=10, y=20)]
    )
    await service.share_view(EDITOR, view.id, ViewShare.user(STRANGER.user_id))

    context = await service.view_context(STRANGER, view.id)
    assert set(context.columns) == {process.orders.id, process.customers.id}
    assert context.stale.entries == ()

    state = await service.view_state(STRANGER, view.id)
    assert state.version == 1
    assert set(state.snapshot.nodes) == {process.orders.id, process.customers.id}
    assert state.snapshot.flows == {}
    assert state.layout.positions[0].x == 10
    assert state.owned is False
    assert (await service.view_state(EDITOR, view.id)).owned is True


async def test_catalog_changed_reaches_bus_subscriber(
    service: CatalogService, process: ProcessSample
) -> None:
    collector, leave = _listen(service, EDITOR)
    try:
        draft = await service.create_draft(EDITOR, "initial")
        await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.LLM)
        version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    finally:
        leave()

    draft_events = [m for m in collector.seen if m.draft_id == draft.id]
    assert [m.action.value for m in draft_events] == ["created", "updated", "deleted"]
    assert [m.version for m in collector.seen if m.version is not None] == [
        version.number
    ]


async def test_sources_follow_the_catalog_rights_and_emit_events(
    service: CatalogService,
) -> None:
    """Читают источники обладатели view_roles, заводят и правят — edit_roles;
    дерево последней версии несёт пометки относительно предыдущей; каждая правка
    источника уходит событием с source_id."""
    collector, leave = _listen(service, EDITOR)
    try:
        with pytest.raises(CatalogRefusalError):
            await service.create_source(
                VIEWER, SourceSpec(kind=SourceKind.POSTGRES, name="prod")
            )

        prod = await service.create_source(
            EDITOR, SourceSpec(kind=SourceKind.POSTGRES, name="prod")
        )
        sample = PgSample()
        await service.write_source_version(EDITOR, prod.id, sample.snapshot())
        await service.write_source_version(EDITOR, prod.id, sample.next_version())

        assert [s.name for s in await service.list_sources(VIEWER)] == ["prod"]
        with pytest.raises(CatalogRefusalError):
            await service.list_sources(STRANGER)

        tables = await service.source_tree(
            VIEWER, prod.id, -1, ("prod", "public", "tables")
        )
        assert {node.label: node.status for node in tables} == {
            "orders": ChangeStatus.MODIFIED,
            "returns": ChangeStatus.ADDED,
        }
        first = await service.source_tree(VIEWER, prod.id, 1, ("prod",))
        assert {node.status for node in first} == {ChangeStatus.UNCHANGED}
        assert len((await service.source_diff(VIEWER, prod.id, 1, 2)).entries) == 4

        planned = await service.create_source(
            EDITOR, SourceSpec(kind=SourceKind.CLICKHOUSE, name="planned", manual=True)
        )
        draft = await service.create_source_draft(EDITOR, planned.id, "shapes")
        obj = ManualObject(
            path=("dwh", "orders"), columns=(ManualColumn(name="id", type="UInt64"),)
        )
        state = await service.append_source_ops(
            EDITOR,
            draft.id,
            0,
            SourceOperationList(root=(AddObject(object=obj),)),
            AuthorVia.LLM,
        )
        assert state.seq == 1
        version = await service.publish_source_draft(EDITOR, draft.id, AuthorVia.USER)
        assert version.version == 1
        with pytest.raises(CatalogRefusalError):
            await service.create_source_draft(VIEWER, planned.id, "nope")
    finally:
        leave()

    source_ids: list[UUID] = []
    for message in collector.seen:
        if message.source_id is None:
            continue

        source_ids.append(message.source_id)

    assert source_ids.count(prod.id) == 3
    assert source_ids.count(planned.id) == 4
