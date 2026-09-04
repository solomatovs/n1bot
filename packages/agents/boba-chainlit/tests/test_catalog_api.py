"""JSON API каталога через HTTP на стенде: коды ответов по контракту модуля api."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
from chainlit.user import PersistedUser
from chainlit_stand import AppConfig
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import sql

from boba.catalog import AddLayer, OperationList, SourceKind
from boba.catalog.samples import PgSample, ProcessSample
from boba.catalog_service import (
    CatalogConfig,
    CatalogService,
    CatalogStore,
    ShareTargetKind,
    SourceStore,
)
from boba.chainlit.catalog.api import CatalogApi, CatalogUrl, SignedIn
from boba.chat.profiles import ChatProfiles
from boba.db.postgres import AsyncPostgresPool
from boba.identity.signin import SignInMetadata
from boba.messaging import MemoryMessageBus

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_api_test"
EDITOR_ID = UUID(int=21)
VIEWER_ID = UUID(int=22)
STRANGER_ID = UUID(int=23)


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("read",), edit_roles=("wrt",)
    )


def _user(user_id: UUID, *roles: str) -> PersistedUser:
    metadata = SignInMetadata(roles=frozenset(roles)).render()
    return PersistedUser(
        id=str(user_id),
        identifier=f"user-{user_id.int}",
        createdAt="2026-01-01T00:00:00Z",
        metadata=metadata,
    )


class Stand:
    """Приложение с маршрутами каталога и подменой пользователя входа."""

    def __init__(self, service: CatalogService, profiles: ChatProfiles) -> None:
        self.app = FastAPI()
        router = APIRouter(prefix=CatalogUrl.PREFIX.value)

        async def source() -> CatalogService:
            return service

        CatalogApi(source, profiles).mount(router)
        self.app.include_router(router)
        self.user: PersistedUser | None = None
        self.app.dependency_overrides[SignedIn.user] = lambda: self.user

    def client(self, user: PersistedUser | None) -> AsyncClient:
        self.user = user
        transport = ASGITransport(app=self.app)
        return AsyncClient(transport=transport, base_url="http://stand")

    @staticmethod
    def url(path: CatalogUrl, **params: Any) -> str:
        return CatalogUrl.PREFIX.value + path.value.format(**params)


@pytest.fixture
async def stand(pool: AsyncPostgresPool, app_config: AppConfig) -> Stand:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    store = CatalogStore(_config(), pool)
    await store.setup()
    sources = SourceStore(_config(), pool)
    await sources.setup()
    service = CatalogService(store, sources, _config(), MemoryMessageBus("test:0"))
    return Stand(service, ChatProfiles(app_config.profiles))


@pytest.fixture
async def process(stand: Stand) -> ProcessSample:
    """Источник prod с версией 1 из образца, заведённый через api; процесс
    ссылается на него."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.SOURCES),
            json={"kind": SourceKind.POSTGRES.value, "name": "prod"},
        )
        assert created.status_code == 200
        source_id = UUID(created.json()["id"])

        snapshot = PgSample().snapshot().model_dump(mode="json")
        written = await client.post(
            stand.url(CatalogUrl.SOURCE_VERSIONS, source_id=source_id),
            json={"snapshot": snapshot},
        )
        assert written.status_code == 200

    return ProcessSample(source_id)


def _ops_body(expected_seq: int, ops: OperationList) -> Mapping[str, Any]:
    return {"expected_seq": expected_seq, "operations": ops.model_dump(mode="json")}


async def test_anonymous_gets_401(stand: Stand) -> None:
    async with stand.client(None) as client:
        response = await client.get(stand.url(CatalogUrl.SNAPSHOT))

    assert response.status_code == 401


async def test_roles_map_to_403(stand: Stand) -> None:
    async with stand.client(_user(STRANGER_ID)) as client:
        response = await client.get(stand.url(CatalogUrl.SNAPSHOT))
        assert response.status_code == 403
        assert "no role to read" in response.json()["detail"]

    async with stand.client(_user(VIEWER_ID, "read")) as client:
        snapshot = await client.get(stand.url(CatalogUrl.SNAPSHOT))
        assert snapshot.status_code == 200
        assert snapshot.json()["layers"] == {}

        draft = await client.post(stand.url(CatalogUrl.DRAFTS), json={"name": "no"})
        assert draft.status_code == 403


async def test_draft_cycle_over_http(stand: Stand, process: ProcessSample) -> None:
    ops = process.ops()

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.DRAFTS), json={"name": "first"}
        )
        assert created.status_code == 200
        draft_id = created.json()["id"]
        assert created.json()["base_version"] == 0

        appended = await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id), json=_ops_body(0, ops)
        )
        assert appended.status_code == 200
        state = appended.json()
        assert state["seq"] == 1
        orders = state["snapshot"]["nodes"][str(process.orders.id)]
        assert orders["ref"]["path"] == ["prod", "public", "orders"]
        assert {entry["status"] for entry in state["diff"]["entries"]} == {"added"}

        conflict = await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id), json=_ops_body(0, ops)
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["current_seq"] == 1

        invalid = await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id), json=_ops_body(1, ops)
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["index"] == 0
        assert "already exists" in invalid.json()["detail"]["reason"]

        malformed = await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id),
            json={"expected_seq": 1, "operations": [{"op": "rename_layer"}]},
        )
        assert malformed.status_code == 422

        listed = await client.get(stand.url(CatalogUrl.DRAFTS))
        assert [d["id"] for d in listed.json()] == [draft_id]

        published = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id)
        )
        assert published.status_code == 200
        assert published.json()["number"] == 1

        closed = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id)
        )
        assert closed.status_code == 409

        snapshot = await client.get(stand.url(CatalogUrl.SNAPSHOT))
        assert str(process.raw.id) in snapshot.json()["layers"]

        versions = await client.get(stand.url(CatalogUrl.VERSIONS))
        assert [v["number"] for v in versions.json()] == [1]

        missing = await client.get(stand.url(CatalogUrl.DRAFT, draft_id=UUID(int=404)))
        assert missing.status_code == 404


async def test_stale_draft_conflicts_and_rebases(
    stand: Stand, process: ProcessSample
) -> None:
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        lagging = (
            await client.post(stand.url(CatalogUrl.DRAFTS), json={"name": "lag"})
        ).json()
        racing = (
            await client.post(stand.url(CatalogUrl.DRAFTS), json={"name": "race"})
        ).json()

        ops = OperationList(root=(AddLayer(layer=process.raw),))
        await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=racing["id"]),
            json=_ops_body(0, ops),
        )
        assert (
            await client.post(
                stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=racing["id"])
            )
        ).status_code == 200

        stale = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=lagging["id"])
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["current_version"] == 1

        rebased = await client.post(
            stand.url(CatalogUrl.DRAFT_REBASE, draft_id=lagging["id"]),
            json={"drop_conflicts": False},
        )
        assert rebased.status_code == 200
        assert rebased.json()["issues"] == []
        assert rebased.json()["draft"]["base_version"] == 1

        discarded = await client.delete(
            stand.url(CatalogUrl.DRAFT, draft_id=lagging["id"])
        )
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "discarded"


async def test_views_layout_and_shares_over_http(
    stand: Stand, process: ProcessSample
) -> None:
    ops = process.ops()

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        draft = await client.post(stand.url(CatalogUrl.DRAFTS), json={"name": "base"})
        draft_id = draft.json()["id"]
        await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id), json=_ops_body(0, ops)
        )
        published = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id)
        )
        assert published.status_code == 200

        created = await client.post(
            stand.url(CatalogUrl.VIEWS),
            json={
                "name": "orders",
                "node_ids": [str(process.orders.id)],
                "layer_ids": [],
            },
        )
        assert created.status_code == 200
        view_id = created.json()["id"]

        layout = await client.put(
            stand.url(CatalogUrl.VIEW_LAYOUT, view_id=view_id),
            json={"positions": [{"node_id": str(process.orders.id), "x": 1.5, "y": 2}]},
        )
        assert layout.status_code == 200
        assert layout.json()["positions"][0]["x"] == 1.5

        shared = await client.post(
            stand.url(CatalogUrl.VIEW_SHARES, view_id=view_id),
            json={"kind": "user", "target": str(STRANGER_ID)},
        )
        assert shared.status_code == 204

        shares = await client.get(stand.url(CatalogUrl.VIEW_SHARES, view_id=view_id))
        assert [s["target"] for s in shares.json()] == [str(STRANGER_ID)]

    async with stand.client(_user(STRANGER_ID)) as client:
        seen = await client.get(stand.url(CatalogUrl.VIEW, view_id=view_id))
        assert seen.status_code == 200
        assert seen.json()["name"] == "orders"

        access = await client.get(stand.url(CatalogUrl.ACCESS))
        assert access.status_code == 200
        assert access.json()["can_view"] is False
        assert access.json()["user_id"] == str(STRANGER_ID)

        state = await client.get(stand.url(CatalogUrl.VIEW_STATE, view_id=view_id))
        assert state.status_code == 200
        assert state.json()["owned"] is False
        assert state.json()["version"] == 1
        assert list(state.json()["snapshot"]["nodes"]) == [str(process.orders.id)]
        assert state.json()["layout"]["positions"][0]["x"] == 1.5
        assert (await client.get(stand.url(CatalogUrl.SNAPSHOT))).status_code == 403

        listed = await client.get(stand.url(CatalogUrl.VIEWS))
        assert [v["id"] for v in listed.json()] == [view_id]

        forbidden = await client.put(
            stand.url(CatalogUrl.VIEW, view_id=view_id),
            json={"name": "mine", "node_ids": [], "layer_ids": []},
        )
        assert forbidden.status_code == 403

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        unshared = await client.delete(
            stand.url(
                CatalogUrl.VIEW_SHARE,
                view_id=view_id,
                kind=ShareTargetKind.USER.value,
                target=str(STRANGER_ID),
            )
        )
        assert unshared.status_code == 200
        assert unshared.json()["deleted"] is True

        deleted = await client.delete(stand.url(CatalogUrl.VIEW, view_id=view_id))
        assert deleted.json()["deleted"] is True

        gone = await client.get(stand.url(CatalogUrl.VIEW, view_id=view_id))
        assert gone.status_code == 404


async def test_disabled_service_gives_503(app_config: AppConfig) -> None:
    async def source() -> CatalogService:
        msg = "[catalog] is disabled: the data catalog is unavailable"
        raise RuntimeError(msg)

    app = FastAPI()
    router = APIRouter(prefix=CatalogUrl.PREFIX.value)
    CatalogApi(source, ChatProfiles(app_config.profiles)).mount(router)
    app.include_router(router)
    app.dependency_overrides[SignedIn.user] = lambda: _user(EDITOR_ID, "wrt")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://stand"
    ) as client:
        response = await client.get(Stand.url(CatalogUrl.SNAPSHOT))

    assert response.status_code == 503


async def test_sources_over_http(stand: Stand) -> None:
    """Источник, две версии из образца, дерево с пометками, карточка, diff;
    читателю всё видно, править нельзя."""
    sample = PgSample()
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.SOURCES),
            json={"kind": "postgres", "name": "prod", "description": "Prod"},
        )
        assert created.status_code == 200
        source_id = created.json()["id"]
        assert created.json()["latest_version"] == 0

        for snapshot in (sample.snapshot(), sample.next_version()):
            written = await client.post(
                stand.url(CatalogUrl.SOURCE_VERSIONS, source_id=source_id),
                json={"snapshot": snapshot.model_dump(mode="json")},
            )
            assert written.status_code == 200

        versions = await client.get(
            stand.url(CatalogUrl.SOURCE_VERSIONS, source_id=source_id)
        )
        assert [v["version"] for v in versions.json()] == [1, 2]

        bound = await client.post(
            stand.url(CatalogUrl.SOURCE_CONNECTIONS, source_id=source_id),
            json={"connection_id": str(STRANGER_ID)},
        )
        assert bound.status_code == 200
        listed = await client.get(
            stand.url(CatalogUrl.SOURCE_CONNECTIONS, source_id=source_id)
        )
        assert [c["connection_id"] for c in listed.json()] == [str(STRANGER_ID)]

    async with stand.client(_user(VIEWER_ID, "read")) as client:
        roots = await client.get(stand.url(CatalogUrl.SOURCE_TREE, source_id=source_id))
        assert roots.status_code == 200
        assert [node["label"] for node in roots.json()] == ["prod"]
        assert roots.json()[0]["status"] == "modified"

        tables = await client.get(
            stand.url(CatalogUrl.SOURCE_TREE, source_id=source_id),
            params=[("path", "prod"), ("path", "public"), ("path", "tables")],
        )
        by_label = {node["label"]: node for node in tables.json()}
        assert by_label["orders"]["status"] == "modified"
        assert by_label["returns"]["status"] == "added"
        assert by_label["orders"]["ref"]["path"] == ["prod", "public", "orders"]

        orders = [("path", "prod"), ("path", "public"), ("path", "orders")]
        card = await client.get(
            stand.url(CatalogUrl.SOURCE_OBJECT, source_id=source_id),
            params=[("kind", "relation"), *orders],
        )
        assert card.status_code == 200
        assert card.json()["card"] == "pg_relation"
        columns = [c["name"] for c in card.json()["columns"]]
        assert columns == ["id", "amount", "created_at", "note"]
        assert card.json()["partitions"][0]["name"] == "orders_2026"

        old_card = await client.get(
            stand.url(CatalogUrl.SOURCE_OBJECT, source_id=source_id),
            params=[("kind", "relation"), *orders, ("version", "1")],
        )
        assert len(old_card.json()["columns"]) == 3

        missing = await client.get(
            stand.url(CatalogUrl.SOURCE_OBJECT, source_id=source_id),
            params=[
                ("kind", "relation"),
                ("path", "prod"),
                ("path", "x"),
                ("path", "y"),
            ],
        )
        assert missing.status_code == 404

        diff = await client.get(
            stand.url(CatalogUrl.SOURCE_DIFF, source_id=source_id),
            params={"old": 1, "new": 2},
        )
        statuses = {
            tuple(e["ref"]["path"]): e["status"] for e in diff.json()["entries"]
        }
        assert statuses[("prod", "public", "customers")] == "removed"

        refused = await client.post(
            stand.url(CatalogUrl.SOURCES), json={"kind": "postgres", "name": "x"}
        )
        assert refused.status_code == 403


async def test_manual_source_drafts_over_http(stand: Stand) -> None:
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        synced = await client.post(
            stand.url(CatalogUrl.SOURCES), json={"kind": "postgres", "name": "prod"}
        )
        not_manual = await client.post(
            stand.url(CatalogUrl.SOURCE_DRAFTS, source_id=synced.json()["id"]),
            json={"name": "no"},
        )
        assert not_manual.status_code == 409

        planned = await client.post(
            stand.url(CatalogUrl.SOURCES),
            json={"kind": "clickhouse", "name": "planned", "manual": True},
        )
        planned_id = planned.json()["id"]

        draft = await client.post(
            stand.url(CatalogUrl.SOURCE_DRAFTS, source_id=planned_id),
            json={"name": "shapes"},
        )
        draft_id = draft.json()["id"]
        ops = [
            {
                "op": "add_object",
                "object": {
                    "path": ["dwh", "orders"],
                    "comment": "Planned",
                    "columns": [{"name": "id", "type": "UInt64", "nullable": False}],
                },
            }
        ]
        appended = await client.post(
            stand.url(CatalogUrl.SOURCE_DRAFT_OPS, draft_id=draft_id),
            json={"expected_seq": 0, "operations": ops},
        )
        assert appended.status_code == 200
        assert appended.json()["seq"] == 1
        assert appended.json()["snapshot"]["kind"] == "clickhouse"

        rejected = await client.post(
            stand.url(CatalogUrl.SOURCE_DRAFT_OPS, draft_id=draft_id),
            json={"expected_seq": 1, "operations": ops},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["index"] == 0

        state = await client.get(stand.url(CatalogUrl.SOURCE_DRAFT, draft_id=draft_id))
        assert state.json()["diff"]["entries"][0]["status"] == "added"

        published = await client.post(
            stand.url(CatalogUrl.SOURCE_DRAFT_PUBLISH, draft_id=draft_id)
        )
        assert published.status_code == 200
        assert published.json()["version"] == 1

        tree = await client.get(
            stand.url(CatalogUrl.SOURCE_TREE, source_id=planned_id),
            params=[("path", "dwh"), ("path", "tables")],
        )
        assert [node["label"] for node in tree.json()] == ["orders"]

        deleted = await client.delete(
            stand.url(CatalogUrl.SOURCE, source_id=planned_id)
        )
        assert deleted.json()["deleted"] is True
