"""CatalogService на живом postgres и памяти шины: права по ролям и шарингу,
события CatalogChanged.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg import sql
from sample_ops import Sample

from boba.catalog import AddLayer, CatalogSnapshot, Layer, OperationList
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogRefusalError,
    CatalogRefusalKind,
    CatalogService,
    CatalogStore,
    NodePosition,
    ViewShare,
    ViewSpec,
)
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope, Subject
from boba.messaging import CatalogChanged, ChangeAction, Envelope, MemoryMessageBus

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
    return CatalogService(store, _config(), MemoryMessageBus("test:0"))


@pytest.fixture
def sample() -> Sample:
    return Sample()


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


async def test_roles_gate_reading_and_editing(
    service: CatalogService, sample: Sample
) -> None:
    with pytest.raises(CatalogRefusalError) as refused:
        await service.snapshot(STRANGER)

    assert refused.value.refusal is CatalogRefusalKind.VIEW_FORBIDDEN

    with pytest.raises(CatalogRefusalError) as refused:
        await service.create_draft(VIEWER, "no")

    assert refused.value.refusal is CatalogRefusalKind.EDIT_FORBIDDEN

    draft = await service.create_draft(EDITOR, "initial")
    state = await service.append_ops(EDITOR, draft.id, 0, sample.ops(), AuthorVia.LLM)
    assert state.seq == 1
    assert state.snapshot == sample.ops().apply(CatalogSnapshot.empty())

    version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
    assert version.number == 1
    assert version.author.user_id == EDITOR.user_id

    assert await service.snapshot(VIEWER) == state.snapshot
    assert [d.id for d in await service.open_drafts(VIEWER)] == []
    assert [v.number for v in await service.versions(VIEWER)] == [1]


async def test_view_access_by_share(service: CatalogService, sample: Sample) -> None:
    view = await service.create_view(
        EDITOR, ViewSpec(name="orders", dataset_ids=(sample.raw_orders.id,))
    )

    with pytest.raises(CatalogRefusalError):
        await service.view(ANALYST, view.id)

    with pytest.raises(CatalogRefusalError):
        await service.share_view(VIEWER, view.id, ViewShare.role("analyst"))

    await service.share_view(EDITOR, view.id, ViewShare.role("analyst"))
    await service.share_view(EDITOR, view.id, ViewShare.user(STRANGER.user_id))

    assert (await service.view(ANALYST, view.id)).id == view.id
    assert (await service.view(STRANGER, view.id)).id == view.id
    assert (await service.view(VIEWER, view.id)).id == view.id
    assert [v.id for v in await service.views(ANALYST)] == [view.id]
    assert (await service.layout(ANALYST, view.id)).positions == ()

    with pytest.raises(CatalogRefusalError) as refused:
        await service.update_view(ANALYST, view.id, ViewSpec(name="mine"))

    assert refused.value.refusal is CatalogRefusalKind.EDIT_FORBIDDEN

    other_editor = _subject(UUID(int=5), "editor")
    with pytest.raises(CatalogRefusalError) as refused:
        await service.update_view(other_editor, view.id, ViewSpec(name="mine"))

    assert refused.value.refusal is CatalogRefusalKind.NOT_OWNER

    assert await service.unshare_view(EDITOR, view.id, ViewShare.role("analyst"))
    with pytest.raises(CatalogRefusalError):
        await service.view(ANALYST, view.id)

    assert await service.delete_view(EDITOR, view.id)


async def test_view_state_slices_the_catalog_for_a_shared_stranger(
    service: CatalogService, sample: Sample
) -> None:
    """Расшаренный вид открывает срез каталога тому, у кого нет ролей на
    каталог; версия и раскладка приходят тем же ответом, владение — только
    у владельца с правом правок."""
    draft = await service.create_draft(EDITOR, "initial")
    await service.append_ops(EDITOR, draft.id, 0, sample.ops(), AuthorVia.LLM)
    await service.publish(EDITOR, draft.id, AuthorVia.USER)

    view = await service.create_view(
        EDITOR, ViewSpec(name="raw", layer_ids=(sample.raw.id,))
    )
    await service.put_layout(
        EDITOR, view.id, [NodePosition(dataset_id=sample.raw_orders.id, x=10, y=20)]
    )
    await service.share_view(EDITOR, view.id, ViewShare.user(STRANGER.user_id))

    with pytest.raises(CatalogRefusalError):
        await service.snapshot(STRANGER)

    state = await service.view_state(STRANGER, view.id)
    assert state.version == 1
    assert set(state.snapshot.datasets) == {
        sample.raw_orders.id,
        sample.raw_items.id,
    }
    assert set(state.snapshot.layers) == {sample.raw.id}
    assert state.snapshot.flows == {}
    assert state.layout.positions[0].x == 10
    assert state.owned is False

    assert (await service.view_state(EDITOR, view.id)).owned is True
    assert (await service.view_state(VIEWER, view.id)).owned is False

    access = service.access(STRANGER)
    assert (access.can_view, access.can_edit) == (False, False)
    assert service.access(VIEWER).can_view is True
    assert service.access(EDITOR).can_edit is True


async def test_catalog_changed_reaches_bus_subscriber(
    service: CatalogService, sample: Sample
) -> None:
    collector = Collector()
    leave = _bus_of(service).subscribe(Scope.user(EDITOR.user_id), collector)
    try:
        draft = await service.create_draft(EDITOR, "initial")
        await service.append_ops(EDITOR, draft.id, 0, sample.ops(), AuthorVia.LLM)
        version = await service.publish(EDITOR, draft.id, AuthorVia.USER)
        view = await service.create_view(EDITOR, ViewSpec(name="all"))
    finally:
        leave()

    expected = [
        CatalogChanged(draft_id=draft.id, action=ChangeAction.CREATED),
        CatalogChanged(draft_id=draft.id, action=ChangeAction.UPDATED),
        CatalogChanged(draft_id=draft.id, action=ChangeAction.DELETED),
        CatalogChanged(version=version.number, action=ChangeAction.CREATED),
        CatalogChanged(view_id=view.id, action=ChangeAction.CREATED),
    ]
    assert collector.seen == expected


async def test_rebase_without_conflicts_notifies(service: CatalogService) -> None:
    lagging = await service.create_draft(EDITOR, "lagging")
    ods = OperationList(root=(AddLayer(layer=Layer(id=UUID(int=103), name="ods")),))
    await service.append_ops(EDITOR, lagging.id, 0, ods, AuthorVia.USER)

    racing = await service.create_draft(EDITOR, "racing")
    await service.publish(EDITOR, racing.id, AuthorVia.USER)

    collector = Collector()
    leave = _bus_of(service).subscribe(Scope.user(EDITOR.user_id), collector)
    try:
        result = await service.rebase(EDITOR, lagging.id, drop_conflicts=False)
    finally:
        leave()

    assert result.issues == ()
    assert result.draft.base_version == 1
    assert collector.seen == [
        CatalogChanged(draft_id=lagging.id, action=ChangeAction.UPDATED)
    ]
