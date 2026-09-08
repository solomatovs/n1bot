"""Сервис каталога на живом Postgres и шине в памяти: права по ролям,
процессы с черновиками над снимком подключения с привязками версий,
публикация, устаревание после новой версии снимка и поднятие привязок,
ссылки на просмотр для гостя, отказ забыть версии занятого подключения,
дерево снимка с пометками, события шины."""

from __future__ import annotations

from uuid import UUID

import pytest

from boba.catalog import (
    CatalogOpError,
    CatalogSnapshot,
    ObjectKind,
    OperationList,
    RemoveFlow,
    RemoveNode,
    RetargetNode,
    SnapshotResolver,
    StaleReason,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogRefusalError,
    CatalogRefusalKind,
    CatalogService,
    ConnectionInfo,
    ConnectionInUseError,
    ProcessSpec,
    SharedNodeNotFoundError,
    ShareNotFoundError,
    SyncSetupError,
    SyncStatus,
    UnknownSourceKindError,
    UpgradeReport,
    UpgradeStatus,
    UpgradeTarget,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.context import Subject
from boba.stand.catalog_ports import StubSyncPorts
from boba.stand.catalog_stand import CatalogStand, ChangeCollector

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONFIG = CatalogStand.config("catalog_service_test", ("viewer",), ("editor",))


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
    stand = await CatalogStand.build(pool, CONFIG, CatalogStand.kinds())
    return stand.service(StubSyncPorts((PG_CONNECTION, WEB_CONNECTION)))


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
    # amount расширена numeric(10,2) → numeric(12,2): потоку не мешает
    assert reasons == {(process.customers.id, StaleReason.OBJECT_REMOVED)}

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


async def _upgrade(
    service: CatalogService,
    subject: Subject,
    target: UpgradeTarget,
    entity_id: UUID | None,
) -> UpgradeReport:
    """Запуск upgrade задачей и его отчёт после завершения."""
    started = await service.start_upgrade(subject, target, entity_id, AuthorVia.USER)
    assert started.status is SyncStatus.RUNNING
    finished = await service.wait_upgrade(started.id)
    assert finished.status is SyncStatus.DONE, finished
    assert finished.done == finished.total
    return await service.upgrade_report(subject, finished.id)


async def _blocked_then_fixed(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    """Процесс над удалённой таблицей: upgrade blocked с проблемой у узла,
    читателю запрещён; поток и узел снимаются в черновике, публикация
    привязывает процесс ко второй версии."""
    await _published(service, process_id, process)
    listed = await service.process(VIEWER, process_id)
    assert listed.pins == {PG_CONNECTION.id: 1}
    assert listed.connections == (PG_CONNECTION.id,)
    assert not listed.behind

    sample = PgSample()
    await service.write_connection_version(
        EDITOR, PG_CONNECTION.id, sample.next_version()
    )
    behind = await service.process(VIEWER, process_id)
    assert behind.behind
    assert behind.attention == 0

    collector = ChangeCollector.listen(service, EDITOR)
    try:
        report = await _upgrade(service, EDITOR, UpgradeTarget.PROCESS, process_id)
    finally:
        collector.leave()

    assert (report.run.total, report.run.moved, report.run.blocked) == (1, 0, 1)
    assert any(m.upgrade_id == report.run.id for m in collector.seen)
    blocked = report.upgrades[0]
    assert blocked.run_id == report.run.id
    assert blocked.status is UpgradeStatus.BLOCKED
    assert blocked.pins_before == {PG_CONNECTION.id: 1}
    assert blocked.pins_after == {PG_CONNECTION.id: 2}
    assert [(p.target.id, p.reason) for p in blocked.problems] == [
        (process.customers.id, StaleReason.OBJECT_REMOVED)
    ]
    assert blocked.version is None
    assert (await service.process(VIEWER, process_id)).attention == 1
    last = await service.last_upgrade(VIEWER, process_id)
    assert last is not None
    assert last.id == blocked.id
    assert (await service.process(VIEWER, process_id)).latest_version == 1

    with pytest.raises(CatalogRefusalError):
        await service.start_upgrade(
            VIEWER, UpgradeTarget.PROCESS, process_id, AuthorVia.USER
        )

    # починка: поток и узел над удалённой таблицей снимаются в черновике
    fix = await service.create_draft(EDITOR, process_id, "fix")
    ops = OperationList(
        root=(
            RemoveFlow(id=process.flow_customers.id),
            RemoveNode(id=process.customers.id),
        )
    )
    await service.append_ops(EDITOR, fix.id, 0, ops, AuthorVia.USER)
    await service.publish(EDITOR, fix.id, AuthorVia.USER)
    fixed = await service.process(VIEWER, process_id)
    assert fixed.latest_version == 2
    assert fixed.pins == {PG_CONNECTION.id: 2}
    assert not fixed.behind
    assert fixed.attention == 0


async def test_upgrade_moves_a_compatible_process_and_blocks_a_broken_one(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    """Upgrade: процесс над удалённой таблицей остаётся на старой привязке с
    записью blocked; после починки новая версия снимка с расширенной колонкой
    переводит процесс сам — новая версия процесса без операций и с новыми
    привязками; upgrade всех переводит только отставшие."""
    await _blocked_then_fixed(service, process, process_id)
    sample = PgSample()

    # ничего не отстаёт: upgrade всех никого не трогает
    nothing = await _upgrade(service, EDITOR, UpgradeTarget.ALL, None)
    assert (nothing.run.total, nothing.run.moved, nothing.run.blocked) == (0, 0, 0)
    assert nothing.upgrades == ()

    # третья версия снимка расширяет amount ещё раз: перевод без вмешательства
    wider = sample.orders_amount.model_copy(update={"type": "numeric(14,2)"})
    second = sample.next_version()
    columns = tuple(
        wider if column.key == sample.orders_amount.key else column
        for column in second.columns
    )
    await service.write_connection_version(
        EDITOR, PG_CONNECTION.id, second.model_copy(update={"columns": columns})
    )
    assert (await service.process(VIEWER, process_id)).behind

    everything = await _upgrade(service, EDITOR, UpgradeTarget.ALL, None)
    assert (everything.run.moved, everything.run.blocked) == (1, 0)
    assert everything.run.target is UpgradeTarget.ALL
    moved = everything.upgrades[0]
    assert moved.status is UpgradeStatus.MOVED
    assert moved.version == 3
    assert moved.pins_after == {PG_CONNECTION.id: 3}
    upgraded = await service.process(VIEWER, process_id)
    assert upgraded.latest_version == 3
    assert not upgraded.behind
    versions = await service.versions(VIEWER, process_id)
    assert versions[-1].operations.root == ()
    assert versions[-1].pins == {PG_CONNECTION.id: 3}
    assert await service.snapshot(
        VIEWER, process_id
    ) == await service.processes.snapshot_at(process_id, 2)

    # процесс без отставания: запуск проходит как moved, записи итога нет
    same = await _upgrade(service, EDITOR, UpgradeTarget.PROCESS, process_id)
    assert (same.run.moved, same.run.blocked) == (1, 0)
    assert same.upgrades == ()
    assert (await service.last_upgrade(VIEWER, process_id)) == moved
    runs = await service.upgrade_runs(VIEWER, process_id, None, 10)
    assert [run.id for run in runs][:2] == [same.run.id, everything.run.id]


async def test_upgrade_of_a_draft_moves_its_pins_or_names_the_broken_column(
    service: CatalogService, process: ProcessSample, process_id: UUID
) -> None:
    """Черновик: удалённая в новой версии колонка пары останавливает upgrade
    с проблемой у узла и у потока, черновик помечен в списке; версия, в которой
    колонка на месте, переводит привязки черновика."""
    await _published(service, process_id, process)
    draft = await service.create_draft(EDITOR, process_id, "wip")
    assert draft.pins == {PG_CONNECTION.id: 1}

    sample = PgSample()
    base = sample.snapshot()
    without_amount = tuple(
        column for column in base.columns if column.key != sample.orders_amount.key
    )
    await service.write_connection_version(
        EDITOR, PG_CONNECTION.id, base.model_copy(update={"columns": without_amount})
    )

    blocked = (await _upgrade(service, EDITOR, UpgradeTarget.DRAFT, draft.id)).upgrades[
        0
    ]
    assert blocked.status is UpgradeStatus.BLOCKED
    assert blocked.draft_id == draft.id
    assert {(p.target.id, p.reason) for p in blocked.problems} == {
        (process.orders.id, StaleReason.COLUMN_REMOVED),
        (process.flow_orders.id, StaleReason.COLUMN_REMOVED),
    }
    assert (await service.draft(VIEWER, draft.id)).pins == {PG_CONNECTION.id: 1}
    mine = await service.my_drafts(EDITOR)
    assert [(d.id, d.behind, d.attention) for d in mine] == [(draft.id, True, 2)]

    await service.write_connection_version(EDITOR, PG_CONNECTION.id, base)
    moved = (await _upgrade(service, EDITOR, UpgradeTarget.DRAFT, draft.id)).upgrades[0]
    assert moved.status is UpgradeStatus.MOVED
    assert moved.pins_after == {PG_CONNECTION.id: 3}
    assert (await service.draft(VIEWER, draft.id)).pins == {PG_CONNECTION.id: 3}
    mine = await service.my_drafts(EDITOR)
    assert [(d.behind, d.attention) for d in mine] == [(False, 0)]


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

    collector = ChangeCollector.listen(service, EDITOR)
    renamed = await service.rename_draft(EDITOR, draft.id, "refunds flow")
    assert renamed.name == "refunds flow"
    collector.leave()
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
    collector = ChangeCollector.listen(service, EDITOR)
    try:
        created = await service.create_process(EDITOR, ProcessSpec(name="orders"))
        draft = await service.create_draft(EDITOR, created.id, "initial")
        await service.append_ops(EDITOR, draft.id, 0, process.ops(), AuthorVia.LLM)
        version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    finally:
        collector.leave()

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
    collector = ChangeCollector.listen(service, EDITOR)
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

        # дерево — объекты выбранной версии без пометок: во второй версии
        # customers ушла, returns появилась; читаются только записи пути
        tables = await service.connection_tree(
            VIEWER, PG_CONNECTION.id, -1, ("prod", "public", "tables")
        )
        assert sorted(node.label for node in tables) == ["orders", "returns"]
        first_tables = await service.connection_tree(
            VIEWER, PG_CONNECTION.id, 1, ("prod", "public", "tables")
        )
        assert sorted(node.label for node in first_tables) == ["customers", "orders"]
        first_tree = await service.connection_tree(
            VIEWER, PG_CONNECTION.id, 1, ("prod",)
        )
        assert all(node.expandable for node in first_tree)
        diff = await service.connection_diff(VIEWER, PG_CONNECTION.id, 1, 2)
        assert len(diff.entries) == 4
    finally:
        collector.leave()

    connection_ids: list[UUID] = []
    for message in collector.seen:
        if message.connection_id is None:
            continue

        connection_ids.append(message.connection_id)

    assert connection_ids.count(PG_CONNECTION.id) == 2
