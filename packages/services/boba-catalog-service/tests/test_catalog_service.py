"""Сервис каталога на живом Postgres и шине в памяти: права по ролям,
процессы с черновиками над снимком подключения с привязками версий,
публикация, устаревание после новой версии снимка и поднятие привязок,
ссылки на просмотр для гостя, отказ забыть версии занятого подключения,
дерево снимка с пометками, события шины."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    ObjectKind,
    OperationList,
    RemoveFlow,
    RemoveNode,
    RetargetNode,
    SnapshotResolver,
    SourceKinds,
    StaleReason,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogRefusalError,
    CatalogRefusalKind,
    CatalogService,
    ConnectionInfo,
    ConnectionInUseError,
    ConnectionStore,
    ProcessSpec,
    ProcessStore,
    SharedNodeNotFoundError,
    ShareNotFoundError,
    SyncSetupError,
    UnknownSourceKindError,
)
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.context import Scope, Subject
from boba.messaging import CatalogChanged, Envelope, MemoryMessageBus
from boba.stand.catalog_ports import StubSyncPorts

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)
"""Реестр видов теста: оба снимка из пакетов драйверов."""

SCHEMA = "catalog_service_test"


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("viewer",), edit_roles=("editor",)
    )


def _subject(user_id: UUID, *roles: str) -> Subject:
    return Subject.of_user(user_id, f"user-{user_id.int}", roles, "test")


PG_CONNECTION = ConnectionInfo(id=UUID(int=71), name="prod-pg", kind="postgres")
WEB_CONNECTION = ConnectionInfo(id=UUID(int=73), name="confluence", kind="web")
EDITOR = _subject(UUID(int=1), "editor")
OTHER_EDITOR = _subject(UUID(int=5), "editor")
VIEWER = _subject(UUID(int=2), "viewer")
STRANGER = _subject(UUID(int=4))


@pytest.fixture
async def service(pool: AsyncPostgresPool) -> CatalogService:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    processes = ProcessStore(_config(), pool)
    await processes.setup()
    connections = ConnectionStore(_config(), KINDS, pool)
    await connections.setup()
    return CatalogService(
        processes,
        connections,
        _config(),
        MemoryMessageBus("test:0"),
        StubSyncPorts((PG_CONNECTION, WEB_CONNECTION)),
    )


@pytest.fixture
async def process(service: CatalogService) -> ProcessSample:
    """Подключение prod-pg с версией 1 из образца; образец процесса ссылается
    на него."""
    await service.write_connection_version(
        EDITOR, PG_CONNECTION.id, PgSample().snapshot()
    )
    return ProcessSample(PG_CONNECTION.id)


@pytest.fixture
async def process_id(service: CatalogService) -> UUID:
    created = await service.create_process(EDITOR, ProcessSpec(name="orders"))
    return created.id


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


async def _published(
    service: CatalogService, process_id: UUID, process: ProcessSample
) -> CatalogSnapshot:
    draft = await service.create_draft(EDITOR, process_id, "initial")
    state = await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)
    return state.snapshot


async def test_roles_gate_reading_and_editing(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    with pytest.raises(CatalogRefusalError) as refused:
        await service.list_processes(STRANGER)

    assert refused.value.refusal is CatalogRefusalKind.VIEW_FORBIDDEN

    with pytest.raises(CatalogRefusalError) as refused:
        await service.create_process(VIEWER, ProcessSpec(name="no"))

    assert refused.value.refusal is CatalogRefusalKind.EDIT_FORBIDDEN

    with pytest.raises(CatalogRefusalError) as refused:
        await service.create_draft(VIEWER, process_id, "no")

    assert refused.value.refusal is CatalogRefusalKind.EDIT_FORBIDDEN

    draft = await service.create_draft(EDITOR, process_id, "initial")
    assert draft.pins == {PG_CONNECTION.id: 1}

    state = await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.LLM)
    assert state.seq == 1
    resolver = SnapshotResolver({PG_CONNECTION.id: PgSample().snapshot()})
    assert state.snapshot == process.ops().apply(CatalogSnapshot.empty(), resolver)

    version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    assert version.number == 1
    assert version.process_id == process_id
    assert version.pins == {PG_CONNECTION.id: 1}

    assert await service.snapshot(VIEWER, process_id) == state.snapshot
    assert [v.number for v in await service.versions(VIEWER, process_id)] == [1]
    listed = await service.list_processes(VIEWER)
    assert [(p.name, p.nodes, p.latest_version) for p in listed] == [("orders", 4, 1)]

    # удалить и расшарить процесс может только владелец с правом на правки
    with pytest.raises(CatalogRefusalError) as refused:
        await service.delete_process(OTHER_EDITOR, process_id)

    assert refused.value.refusal is CatalogRefusalKind.NOT_OWNER
    assert await service.delete_process(EDITOR, process_id) is True
    assert await service.list_processes(VIEWER) == []


async def test_portions_are_checked_against_pinned_snapshots(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    draft = await service.create_draft(EDITOR, process_id, "checked")
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


async def test_new_snapshot_version_marks_staleness_and_pins_can_bump(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    await _published(service, process_id, process)
    assert (await service.staleness(VIEWER, process_id)).entries == ()

    await service.write_connection_version(
        EDITOR, PG_CONNECTION.id, PgSample().next_version()
    )

    stale = await service.staleness(VIEWER, process_id)
    reasons = {(s.target.id, s.reason) for s in stale.entries}
    assert (process.customers.id, StaleReason.OBJECT_REMOVED) in reasons
    assert (process.flow_orders.id, StaleReason.COLUMN_CHANGED) in reasons

    context = await service.context(VIEWER, process_id)
    assert context.pins == {PG_CONNECTION.id: 1}
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

    lagging = await service.create_draft(EDITOR, process_id, "lagging")
    assert lagging.pins == {PG_CONNECTION.id: 2}
    assert (await service.draft_staleness(VIEWER, lagging.id)).entries == ()

    bump = await service.bump_pins(EDITOR, lagging.id)
    assert bump.draft.pins == {PG_CONNECTION.id: 2}
    assert any("customers" in violation for violation in bump.violations)


async def test_forgotten_versions_in_pins_do_not_break_the_context(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    """Версии подключения забыты после публикации, узлов над ним больше нет:
    привязка на него пропускается, контекст считается по оставшимся."""
    await _published(service, process_id, process)

    with pytest.raises(ConnectionInUseError) as busy:
        await service.forget_versions(EDITOR, PG_CONNECTION.id)

    assert "4 node(s) of process 'orders'" in str(busy.value)
    assert "prod-pg" in str(busy.value)
    assert "4 node(s) of process 'orders'" in await service.holding_reason(
        PG_CONNECTION.id
    )

    cleanup = await service.create_draft(EDITOR, process_id, "cleanup")
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

    reason = await service.holding_reason(PG_CONNECTION.id)
    assert "has 1 catalog version(s); forget them first" in reason
    assert await service.forget_versions(EDITOR, PG_CONNECTION.id) == 1
    assert await service.holding_reason(PG_CONNECTION.id) == ""

    context = await service.context(VIEWER, process_id)
    assert context.pins == {PG_CONNECTION.id: 1}
    assert context.columns == {}
    assert context.stale.entries == ()
    assert (await service.create_draft(EDITOR, process_id, "after")).pins == {}


async def test_open_draft_nodes_hold_the_connection_too(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    draft = await service.create_draft(EDITOR, process_id, "wip")
    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)

    with pytest.raises(ConnectionInUseError) as busy:
        await service.forget_versions(EDITOR, PG_CONNECTION.id)

    assert "4 node(s) of draft 'wip' of process 'orders'" in str(busy.value)

    await service.discard_draft(EDITOR, draft.id)
    assert await service.forget_versions(EDITOR, PG_CONNECTION.id) == 1


async def test_draft_of_a_new_process_lives_with_its_author_until_published(
    service: CatalogService, process: ProcessSample
) -> None:
    """Черновик без процесса виден только в своих черновиках автора, держит
    подключение под именем черновика, переименование шлёт событие, публикация
    рождает процесс."""
    draft = await service.create_draft(EDITOR, None, "refunds")
    assert draft.process_id is None
    assert [entry.id for entry in await service.my_drafts(EDITOR)] == [draft.id]
    assert await service.my_drafts(OTHER_EDITOR) == []

    await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.USER)
    with pytest.raises(ConnectionInUseError) as busy:
        await service.forget_versions(EDITOR, PG_CONNECTION.id)

    assert "4 node(s) of draft 'refunds' of process 'refunds'" in str(busy.value)

    collector, stop = _listen(service, EDITOR)
    renamed = await service.rename_draft(EDITOR, draft.id, "refunds flow")
    assert renamed.name == "refunds flow"
    stop()
    assert [message.draft_id for message in collector.seen] == [draft.id]

    version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    assert version.number == 1
    created = await service.process(EDITOR, version.process_id)
    assert created.name == "refunds flow"
    assert created.owner_id == EDITOR.user_id
    assert await service.my_drafts(EDITOR) == []


async def test_share_link_opens_the_published_process_to_a_guest(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    published = await _published(service, process_id, process)

    with pytest.raises(CatalogRefusalError) as refused:
        await service.share_process(OTHER_EDITOR, process_id)

    assert refused.value.refusal is CatalogRefusalKind.NOT_OWNER

    share = await service.share_process(EDITOR, process_id)
    assert [s.token for s in await service.shares(EDITOR, process_id)] == [share.token]

    shared = await service.shared_process(share.token)
    assert shared.process.id == process_id
    assert shared.snapshot == published
    assert shared.context.pins == {PG_CONNECTION.id: 1}
    assert [c.name for c in shared.context.columns[process.orders.id]] == [
        "id",
        "amount",
        "created_at",
    ]

    card = await service.shared_object(share.token, process.orders.id)
    assert card.ref == process.orders.ref
    with pytest.raises(SharedNodeNotFoundError):
        await service.shared_object(share.token, UUID(int=0x9999))

    with pytest.raises(CatalogRefusalError):
        await service.revoke_share(OTHER_EDITOR, share.token)

    await service.revoke_share(EDITOR, share.token)
    with pytest.raises(ShareNotFoundError):
        await service.shared_process(share.token)


async def test_catalog_changed_reaches_bus_subscriber(
    service: CatalogService, process: ProcessSample
) -> None:
    collector, leave = _listen(service, EDITOR)
    try:
        created = await service.create_process(EDITOR, ProcessSpec(name="orders"))
        draft = await service.create_draft(EDITOR, created.id, "initial")
        await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.LLM)
        version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    finally:
        leave()

    draft_events = [m for m in collector.seen if m.draft_id == draft.id]
    assert [m.action.value for m in draft_events] == ["created", "updated", "deleted"]
    process_events = [m for m in collector.seen if m.process_id == created.id]
    assert [(m.action.value, m.version) for m in process_events] == [
        ("created", None),
        ("created", version.number),
    ]


async def test_connection_snapshots_follow_the_catalog_rights_and_emit_events(
    service: CatalogService,
) -> None:
    """Читают снимки обладатели view_roles, пишут — edit_roles; дерево
    последней версии несёт пометки относительно предыдущей; версия уходит
    событием с connection_id; у вида без снимка версии не бывает."""
    collector, leave = _listen(service, EDITOR)
    try:
        with pytest.raises(CatalogRefusalError):
            await service.write_connection_version(
                VIEWER, PG_CONNECTION.id, PgSample().snapshot()
            )

        with pytest.raises(SyncSetupError):
            await service.write_connection_version(
                EDITOR, UUID(int=404), PgSample().snapshot()
            )

        with pytest.raises(UnknownSourceKindError):
            await service.write_connection_version(
                EDITOR, WEB_CONNECTION.id, PgSample().snapshot()
            )

        sample = PgSample()
        first = await service.write_connection_version(
            EDITOR, PG_CONNECTION.id, sample.snapshot()
        )
        assert first.connection_name == "prod-pg"
        await service.write_connection_version(
            EDITOR, PG_CONNECTION.id, sample.next_version()
        )

        synced = await service.synced_connections(VIEWER)
        assert [(s.name, s.kind, s.latest_version) for s in synced] == [
            ("prod-pg", "postgres", 2)
        ]
        with pytest.raises(CatalogRefusalError):
            await service.synced_connections(STRANGER)

        tables = await service.connection_tree(
            VIEWER, PG_CONNECTION.id, -1, ("prod", "public", "tables")
        )
        assert {node.label: node.status for node in tables} == {
            "orders": ChangeStatus.MODIFIED,
            "returns": ChangeStatus.ADDED,
        }
        first_tree = await service.connection_tree(
            VIEWER, PG_CONNECTION.id, 1, ("prod",)
        )
        assert {node.status for node in first_tree} == {ChangeStatus.UNCHANGED}
        diff = await service.connection_diff(VIEWER, PG_CONNECTION.id, 1, 2)
        assert len(diff.entries) == 4
    finally:
        leave()

    connection_ids: list[UUID] = []
    for message in collector.seen:
        if message.connection_id is None:
            continue

        connection_ids.append(message.connection_id)

    assert connection_ids.count(PG_CONNECTION.id) == 2
