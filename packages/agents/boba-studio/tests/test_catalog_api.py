"""JSON API каталога studio через HTTP на стенде api: коды ответов по
контракту модуля boba.studio.catalog.api."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets as std_secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from pydantic import SecretStr
from studio_stand import ApiStand

from boba.catalog import (
    AddGroup,
    OperationList,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import CatalogService, ConnectionInfo, SyncPorts
from boba.chat.profiles import ChatProfiles
from boba.connection_broker.api import ConnectionUrl
from boba.connection_broker.service import UserConnectionsService
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.store import ConnectionStore as BrokerStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.profile import GrantTarget, StoredRole
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.api import AuthenticatedUser
from boba.identity.errors import ServiceDisabledError
from boba.identity.signin import SignInMetadata
from boba.stand.catalog_ports import (
    FakeConnections,
    FakeSyncPorts,
    NoSyncTools,
    StubSyncPorts,
)
from boba.stand.catalog_stand import CatalogStand
from boba.stand.refs import StandRefs
from boba.studio.api.app import ApiExtras
from boba.studio.catalog.api import CatalogApi, CatalogUrl
from boba.studio.catalog.sync_ports import (
    BrokerConnectionDirectory,
    CatalogHoldGuard,
)
from boba.studio.config import StudioAppConfig
from boba.transport.http.profile import HttpConnection

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONFIG = CatalogStand.config("catalog_api_test", ("read",), ("wrt",))
EDITOR_ID = UUID(int=21)
VIEWER_ID = UUID(int=22)
STRANGER_ID = UUID(int=23)
OTHER_EDITOR_ID = UUID(int=24)
PG_CONNECTION = FakeConnections.info("prod-pg", "postgres")
CONNECTION_ID = PG_CONNECTION.id
CH_CONNECTION = ConnectionInfo(id=UUID(int=78), name="dwh-ch", kind="clickhouse")
ORACLE_CONNECTION = ConnectionInfo(id=UUID(int=79), name="ora", kind="oracle")
STAND_CONNECTIONS = (PG_CONNECTION, CH_CONNECTION, ORACLE_CONNECTION)


def _user(user_id: UUID, *roles: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=user_id,
        identifier=f"user-{user_id.int}",
        sign_in=SignInMetadata(roles=frozenset(roles)),
    )


class Stand(ApiStand):
    """Приложение api studio с каталогом и общим API соединений."""

    def __init__(
        self,
        service: CatalogService,
        profiles: ChatProfiles,
        connections: BrokerStore | None = None,
    ) -> None:
        self.service = service

        async def source() -> CatalogService:
            return service

        def store() -> BrokerStore:
            if connections is None:
                msg = "the catalog api stand carries no connection store"
                raise ServiceDisabledError("connections", msg)

            return connections

        extras = ApiExtras(
            mounts=(CatalogApi(source),),
            delete_guards=(CatalogHoldGuard(source),),
        )
        super().__init__(StandRefs.of(store, lambda: None), profiles, extras=extras)

    @staticmethod
    def url(path: CatalogUrl, **params: Any) -> str:
        return ApiStand.api_url(CatalogUrl.PREFIX) + path.value.format(**params)


@pytest.fixture
async def stand(pool: AsyncPostgresPool, studio_config: StudioAppConfig) -> Stand:
    catalog = await CatalogStand.build(pool, CONFIG, CatalogStand.kinds())
    service = catalog.service(StubSyncPorts(STAND_CONNECTIONS))
    return Stand(service, ChatProfiles(studio_config.profiles))


@pytest.fixture
async def sync_stand(
    pool: AsyncPostgresPool,
    studio_config: StudioAppConfig,
    tmp_path: Path,
    test_postgres: PostgresConfig,
) -> Stand:
    """Стенд с фейком снятия: роль wrt и профиль по умолчанию видят инструмент."""
    catalog = await CatalogStand.build(pool, CONFIG, CatalogStand.fake_kinds())
    profiles = ChatProfiles(studio_config.profiles)
    site = catalog.fake_site(tmp_path, "wrt", profiles.default_name(), test_postgres)
    ports = FakeSyncPorts(site, (PG_CONNECTION,), (EDITOR_ID,))
    return Stand(catalog.service(ports), profiles)


@pytest.fixture
async def process(stand: Stand) -> ProcessSample:
    """Подключение prod-pg с версией 1 из образца, записанной через api;
    образец процесса ссылается на него."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        snapshot = PgSample().snapshot().model_dump(mode="json")
        written = await client.post(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID),
            json={"snapshot": snapshot},
        )
        assert written.status_code == 200, written.text

    return ProcessSample(CONNECTION_ID)


@pytest.fixture
async def process_id(stand: Stand) -> str:
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.PROCESSES), json={"name": "orders"}
        )
        assert created.status_code == 200, created.text

    return str(created.json()["id"])


def _ops_body(expected_seq: int, ops: OperationList) -> Mapping[str, Any]:
    return {"expected_seq": expected_seq, "operations": ops.model_dump(mode="json")}


async def test_anonymous_gets_401(stand: Stand) -> None:
    async with stand.client(None) as client:
        response = await client.get(stand.url(CatalogUrl.PROCESSES))

    assert response.status_code == 401


async def test_roles_map_to_403(stand: Stand) -> None:
    """Без ролей вход studio не даёт профиля — 403 ещё на входе; с чужой
    ролью каталог отказывает сам."""
    async with stand.client(_user(STRANGER_ID)) as client:
        response = await client.get(stand.url(CatalogUrl.PROCESSES))
        assert response.status_code == 403

    async with stand.client(_user(STRANGER_ID, "guest")) as client:
        response = await client.get(stand.url(CatalogUrl.PROCESSES))
        assert response.status_code == 403
        assert "no role to read" in response.json()["detail"]

    async with stand.client(_user(VIEWER_ID, "read")) as client:
        listed = await client.get(stand.url(CatalogUrl.PROCESSES))
        assert listed.status_code == 200
        assert listed.json() == []

        created = await client.post(
            stand.url(CatalogUrl.PROCESSES), json={"name": "no"}
        )
        assert created.status_code == 403


async def test_processes_over_http(stand: Stand) -> None:
    """Процессы: создание, занятое имя 409, правка, список со счётчиками,
    удаление владельцем, чужим — 403."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.PROCESSES),
            json={"name": "orders", "description": "sales"},
        )
        assert created.status_code == 200, created.text
        process_id = created.json()["id"]
        assert created.json()["owner_id"] == str(EDITOR_ID)
        assert created.json()["latest_version"] == 0
        assert created.json()["nodes"] == 0

        taken = await client.post(
            stand.url(CatalogUrl.PROCESSES), json={"name": "orders"}
        )
        assert taken.status_code == 409
        assert "already exists" in taken.json()["detail"]

        renamed = await client.put(
            stand.url(CatalogUrl.PROCESS, process_id=process_id),
            json={"name": "orders2", "description": "sales"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "orders2"

        fetched = await client.get(stand.url(CatalogUrl.PROCESS, process_id=process_id))
        assert fetched.json()["name"] == "orders2"

        missing = await client.get(
            stand.url(CatalogUrl.PROCESS, process_id=UUID(int=404))
        )
        assert missing.status_code == 404

    async with stand.client(_user(OTHER_EDITOR_ID, "wrt")) as client:
        refused = await client.delete(
            stand.url(CatalogUrl.PROCESS, process_id=process_id)
        )
        assert refused.status_code == 403
        assert "only the owner" in refused.json()["detail"]

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        deleted = await client.delete(
            stand.url(CatalogUrl.PROCESS, process_id=process_id)
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert (await client.get(stand.url(CatalogUrl.PROCESSES))).json() == []


async def test_draft_cycle_over_http(
    stand: Stand, process: ProcessSample, process_id: str
) -> None:
    ops = process.ops()

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.DRAFTS),
            json={"process_id": process_id, "name": "first"},
        )
        assert created.status_code == 200
        draft_id = created.json()["id"]
        assert created.json()["base_version"] == 0
        assert created.json()["process_id"] == process_id

        appended = await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id), json=_ops_body(0, ops)
        )
        assert appended.status_code == 200
        state = appended.json()
        assert state["seq"] == 1
        orders = state["snapshot"]["nodes"][str(process.orders.id)]
        assert orders["ref"]["path"] == ["prod", "public", "orders"]
        assert orders["ref"]["connection_id"] == str(CONNECTION_ID)
        flow = state["snapshot"]["flows"][str(process.flow_orders.id)]
        assert flow["columns"][0] == {"from_column": "id", "to_column": "id"}
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
            json={"expected_seq": 1, "operations": [{"op": "rename_group"}]},
        )
        assert malformed.status_code == 422

        listed = await client.get(stand.url(CatalogUrl.DRAFTS))
        assert [d["id"] for d in listed.json()] == [draft_id]
        processes = await client.get(stand.url(CatalogUrl.PROCESSES))
        assert processes.json()[0]["open_drafts"] == 1

        renamed = await client.put(
            stand.url(CatalogUrl.DRAFT, draft_id=draft_id), json={"name": "first!"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "first!"

        published = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id)
        )
        assert published.status_code == 200
        assert published.json()["number"] == 1
        assert published.json()["process_id"] == process_id

        closed = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id)
        )
        assert closed.status_code == 409

        snapshot = await client.get(
            stand.url(CatalogUrl.PROCESS_SNAPSHOT, process_id=process_id)
        )
        assert str(process.raw.id) in snapshot.json()["groups"]

        versions = await client.get(
            stand.url(CatalogUrl.PROCESS_VERSIONS, process_id=process_id)
        )
        assert [v["number"] for v in versions.json()] == [1]
        assert versions.json()[0]["pins"] == {str(CONNECTION_ID): 1}

        missing = await client.get(stand.url(CatalogUrl.DRAFT, draft_id=UUID(int=404)))
        assert missing.status_code == 404


async def test_context_staleness_and_pins_over_http(
    stand: Stand, process: ProcessSample, process_id: str
) -> None:
    """Контекст черновика несёт привязки, колонки узлов и пустое устаревание;
    upgrade без новых версий ничего не меняет; после публикации контекст и
    устаревание есть у опубликованного процесса."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.DRAFTS),
            json={"process_id": process_id, "name": "ctx"},
        )
        draft_id = created.json()["id"]
        await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id),
            json=_ops_body(0, process.ops()),
        )

        context = await client.get(
            stand.url(CatalogUrl.DRAFT_CONTEXT, draft_id=draft_id)
        )
        assert context.status_code == 200
        assert context.json()["pins"] == {str(CONNECTION_ID): 1}
        columns = context.json()["columns"][str(process.orders.id)]
        assert [c["name"] for c in columns] == ["id", "amount", "created_at"]
        assert context.json()["stale"]["entries"] == []

        staleness = await client.get(
            stand.url(CatalogUrl.DRAFT_STALENESS, draft_id=draft_id)
        )
        assert staleness.status_code == 200
        assert staleness.json()["entries"] == []

        # upgrade — задача: запуск running, итог по id запуска; черновик без
        # отставания проходит как moved, записи итога у него нет
        started = await client.post(
            stand.url(CatalogUrl.DRAFT_UPGRADE, draft_id=draft_id)
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["id"]
        assert started.json()["target"] == "draft"
        for _ in range(50):
            run = await client.get(stand.url(CatalogUrl.UPGRADE, run_id=run_id))
            if run.json()["status"] != "running":
                break

            await asyncio.sleep(0.1)

        assert run.json()["status"] == "done", run.text
        assert (run.json()["moved"], run.json()["blocked"]) == (1, 0)
        report = await client.get(stand.url(CatalogUrl.UPGRADE_REPORT, run_id=run_id))
        assert report.status_code == 200
        assert report.json()["upgrades"] == []
        listed = await client.get(
            stand.url(CatalogUrl.UPGRADES), params={"draft_id": draft_id}
        )
        assert [item["id"] for item in listed.json()] == [run_id]
        missing = await client.get(
            stand.url(CatalogUrl.DRAFT_UPGRADE, draft_id=draft_id)
        )
        assert missing.status_code == 404
        closed = await client.delete(stand.url(CatalogUrl.UPGRADE, run_id=run_id))
        assert closed.status_code == 409

        await client.post(stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id))

        published_context = await client.get(
            stand.url(CatalogUrl.PROCESS_CONTEXT, process_id=process_id)
        )
        assert published_context.status_code == 200
        assert str(process.orders.id) in published_context.json()["columns"]
        staleness = await client.get(
            stand.url(CatalogUrl.PROCESS_STALENESS, process_id=process_id)
        )
        assert staleness.json() == {"entries": []}


async def test_source_kinds_come_from_the_registry(stand: Stand) -> None:
    """Виды подключений — kind типов соединений с установленным снимком;
    версия для подключения неизвестного вида отвергается 422 с перечнем
    установленных, снимок чужого вида — тоже."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        kinds = await client.get(stand.url(CatalogUrl.SOURCE_KINDS))
        assert kinds.status_code == 200
        assert kinds.json() == ["clickhouse", "postgres"]

        snapshot = PgSample().snapshot().model_dump(mode="json")
        refused = await client.post(
            stand.url(
                CatalogUrl.CONNECTION_VERSIONS, connection_id=ORACLE_CONNECTION.id
            ),
            json={"snapshot": snapshot},
        )
        assert refused.status_code == 422
        assert "connection kind 'oracle' has no snapshot installed" in refused.text

        rejected = await client.post(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID),
            json={"snapshot": {"kind": "oracle"}},
        )
        assert rejected.status_code == 422
        assert "source kind 'oracle' has no snapshot class" in rejected.text

        unseen = await client.post(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=uuid4()),
            json={"snapshot": snapshot},
        )
        assert unseen.status_code == 422
        assert "not visible" in unseen.json()["detail"]

        mismatch = await client.post(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CH_CONNECTION.id),
            json={"snapshot": snapshot},
        )
        assert mismatch.status_code == 409
        assert "the new snapshot is postgres" in mismatch.json()["detail"]


async def test_draft_without_a_process_publishes_a_new_one(
    stand: Stand, process: ProcessSample
) -> None:
    """POST /drafts без process_id — черновик нового процесса: в своих
    черновиках, чужому не виден, публикация создаёт процесс с его именем."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        created = await client.post(
            stand.url(CatalogUrl.DRAFTS), json={"process_id": None, "name": "refunds"}
        )
        assert created.status_code == 200
        draft = created.json()
        assert draft["process_id"] is None
        assert draft["base_version"] == 0

        state = await client.get(stand.url(CatalogUrl.DRAFT, draft_id=draft["id"]))
        assert state.json()["snapshot"]["nodes"] == {}

        ops = OperationList(root=(AddGroup(group=process.raw),))
        appended = await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft["id"]),
            json=_ops_body(0, ops),
        )
        assert appended.status_code == 200

        published = await client.post(
            stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft["id"])
        )
        assert published.status_code == 200
        assert published.json()["number"] == 1
        process_id = published.json()["process_id"]

        got = await client.get(stand.url(CatalogUrl.PROCESS, process_id=process_id))
        assert got.json()["name"] == "refunds"
        assert got.json()["latest_version"] == 1
        assert (await client.get(stand.url(CatalogUrl.DRAFTS))).json() == []

    async with stand.client(_user(OTHER_EDITOR_ID, "wrt")) as other:
        assert (await other.get(stand.url(CatalogUrl.DRAFTS))).json() == []


async def test_stale_draft_conflicts_and_rebases(
    stand: Stand, process: ProcessSample, process_id: str
) -> None:
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        drafts_url = stand.url(CatalogUrl.DRAFTS)
        lagging = (
            await client.post(
                drafts_url, json={"process_id": process_id, "name": "lag"}
            )
        ).json()
        racing = (
            await client.post(
                drafts_url, json={"process_id": process_id, "name": "race"}
            )
        ).json()

        ops = OperationList(root=(AddGroup(group=process.raw),))
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


async def test_share_link_serves_the_process_to_a_guest(
    stand: Stand, process: ProcessSample, process_id: str
) -> None:
    """Ссылка на просмотр: владелец выпускает и отзывает, гость без входа
    читает опубликованный процесс и карточки узлов, после отзыва — 404."""
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        draft = await client.post(
            stand.url(CatalogUrl.DRAFTS),
            json={"process_id": process_id, "name": "sh"},
        )
        draft_id = draft.json()["id"]
        await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft_id),
            json=_ops_body(0, process.ops()),
        )
        await client.post(stand.url(CatalogUrl.DRAFT_PUBLISH, draft_id=draft_id))

        shared = await client.post(
            stand.url(CatalogUrl.PROCESS_SHARES, process_id=process_id)
        )
        assert shared.status_code == 200, shared.text
        token = shared.json()["token"]
        listed = await client.get(
            stand.url(CatalogUrl.PROCESS_SHARES, process_id=process_id)
        )
        assert [s["token"] for s in listed.json()] == [token]

    async with stand.client(_user(OTHER_EDITOR_ID, "wrt")) as client:
        refused = await client.post(
            stand.url(CatalogUrl.PROCESS_SHARES, process_id=process_id)
        )
        assert refused.status_code == 403

    async with stand.client(None) as guest:
        page = await guest.get(stand.url(CatalogUrl.SHARED, token=token))
        assert page.status_code == 200, page.text
        assert page.json()["process"]["id"] == process_id
        assert str(process.orders.id) in page.json()["snapshot"]["nodes"]
        assert str(process.orders.id) in page.json()["context"]["columns"]

        card = await guest.get(
            stand.url(CatalogUrl.SHARED_OBJECT, token=token, node_id=process.orders.id)
        )
        assert card.status_code == 200
        assert card.json()["card"] == "pg_relation"
        assert [c["name"] for c in card.json()["columns"]] == [
            "id",
            "amount",
            "created_at",
        ]

        outside = await guest.get(
            stand.url(CatalogUrl.SHARED_OBJECT, token=token, node_id=uuid4())
        )
        assert outside.status_code == 404

        unknown = await guest.get(stand.url(CatalogUrl.SHARED, token="nope"))
        assert unknown.status_code == 404

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        revoked = await client.delete(stand.url(CatalogUrl.SHARE, token=token))
        assert revoked.status_code == 200
        assert revoked.json()["revoked_at"] is not None

    async with stand.client(None) as guest:
        gone = await guest.get(stand.url(CatalogUrl.SHARED, token=token))
        assert gone.status_code == 404


async def test_disabled_service_gives_503(studio_config: StudioAppConfig) -> None:
    async def source() -> CatalogService:
        msg = "[catalog] is disabled: the data catalog is unavailable"
        raise ServiceDisabledError("catalog", msg)

    extras = ApiExtras(mounts=(CatalogApi(source),))
    stand = ApiStand(
        StandRefs.none(), ChatProfiles(studio_config.profiles), extras=extras
    )
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        response = await client.get(Stand.url(CatalogUrl.PROCESSES))

    assert response.status_code == 503


async def test_connection_snapshots_over_http(stand: Stand) -> None:
    """Две версии снимка подключения из образца, список синхронизированных,
    дерево с пометками, карточка, diff, забытые версии; читателю всё видно,
    писать нельзя."""
    sample = PgSample()
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        empty = await client.get(stand.url(CatalogUrl.SYNCED))
        assert empty.json() == []

        for snapshot in (sample.snapshot(), sample.next_version()):
            written = await client.post(
                stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID),
                json={"snapshot": snapshot.model_dump(mode="json")},
            )
            assert written.status_code == 200, written.text
            assert written.json()["connection_name"] == "prod-pg"

        versions = await client.get(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID)
        )
        assert [v["version"] for v in versions.json()] == [1, 2]

        synced = await client.get(stand.url(CatalogUrl.SYNCED))
        assert [(s["name"], s["kind"], s["latest_version"]) for s in synced.json()] == [
            ("prod-pg", "postgres", 2)
        ]

    async with stand.client(_user(VIEWER_ID, "read")) as client:
        roots = await client.get(
            stand.url(CatalogUrl.CONNECTION_TREE, connection_id=CONNECTION_ID)
        )
        assert roots.status_code == 200
        assert [node["label"] for node in roots.json()] == ["prod"]
        assert roots.json()[0]["expandable"] is True

        tables = await client.get(
            stand.url(CatalogUrl.CONNECTION_TREE, connection_id=CONNECTION_ID),
            params=[("path", "prod"), ("path", "public"), ("path", "tables")],
        )
        by_label = {node["label"]: node for node in tables.json()}
        assert sorted(by_label) == ["orders", "returns"]
        assert by_label["orders"]["expandable"] is True
        assert by_label["returns"]["expandable"] is False
        assert by_label["orders"]["ref"]["path"] == ["prod", "public", "orders"]
        assert by_label["orders"]["ref"]["connection_id"] == str(CONNECTION_ID)

        orders = [("path", "prod"), ("path", "public"), ("path", "orders")]
        card = await client.get(
            stand.url(CatalogUrl.CONNECTION_OBJECT, connection_id=CONNECTION_ID),
            params=[("kind", "relation"), *orders],
        )
        assert card.status_code == 200
        assert card.json()["card"] == "pg_relation"
        columns = [c["name"] for c in card.json()["columns"]]
        assert columns == ["id", "amount", "created_at", "note"]
        assert card.json()["partitions"][0]["name"] == "orders_2026"

        old_card = await client.get(
            stand.url(CatalogUrl.CONNECTION_OBJECT, connection_id=CONNECTION_ID),
            params=[("kind", "relation"), *orders, ("version", "1")],
        )
        assert len(old_card.json()["columns"]) == 3

        missing = await client.get(
            stand.url(CatalogUrl.CONNECTION_OBJECT, connection_id=CONNECTION_ID),
            params=[
                ("kind", "relation"),
                ("path", "prod"),
                ("path", "x"),
                ("path", "y"),
            ],
        )
        assert missing.status_code == 404

        never = await client.get(
            stand.url(CatalogUrl.CONNECTION_TREE, connection_id=CH_CONNECTION.id)
        )
        assert never.status_code == 404
        assert "no snapshot versions" in never.json()["detail"]

        diff = await client.get(
            stand.url(CatalogUrl.CONNECTION_DIFF, connection_id=CONNECTION_ID),
            params={"old": 1, "new": 2},
        )
        statuses = {
            tuple(e["ref"]["path"]): e["status"] for e in diff.json()["entries"]
        }
        assert statuses[("prod", "public", "customers")] == "removed"

        refused = await client.delete(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID)
        )
        assert refused.status_code == 403

    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        forgotten = await client.delete(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID)
        )
        assert forgotten.status_code == 200
        assert forgotten.json()["versions"] == 2
        assert (await client.get(stand.url(CatalogUrl.SYNCED))).json() == []


async def test_versions_of_a_used_connection_cannot_be_forgotten(
    stand: Stand, process: ProcessSample, process_id: str
) -> None:
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        draft = await client.post(
            stand.url(CatalogUrl.DRAFTS),
            json={"process_id": process_id, "name": "wip"},
        )
        await client.post(
            stand.url(CatalogUrl.DRAFT_OPS, draft_id=draft.json()["id"]),
            json=_ops_body(0, process.ops()),
        )

        held = await client.delete(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID)
        )
        assert held.status_code == 409
        assert "4 node(s) of draft 'wip' of process 'orders'" in held.json()["detail"]


async def test_sync_over_http(sync_stand: Stand) -> None:
    """Синхронизация через api: старт, запись с прогрессом, версия из порций,
    отмена закрытой синхронизации даёт 409, чужое подключение — 422."""
    stand = sync_stand
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        unseen = await client.post(
            stand.url(CatalogUrl.CONNECTION_SYNCS, connection_id=uuid4()),
            json={},
        )
        assert unseen.status_code == 422
        assert "not visible" in unseen.json()["detail"]

        started = await client.post(
            stand.url(CatalogUrl.CONNECTION_SYNCS, connection_id=CONNECTION_ID),
            json={"schemas": []},
        )
        assert started.status_code == 200, started.text
        sync_id = started.json()["id"]
        assert started.json()["status"] == "running"
        assert started.json()["connection_name"] == "prod-pg"

        finished = await stand.service.syncs.wait(UUID(sync_id))
        assert finished.status.value == "done", finished.error

        fetched = await client.get(stand.url(CatalogUrl.SYNC, sync_id=sync_id))
        assert fetched.status_code == 200
        assert fetched.json()["version"] == 1
        assert fetched.json()["objects_done"] == fetched.json()["objects_total"]

        listed = await client.get(
            stand.url(CatalogUrl.CONNECTION_SYNCS, connection_id=CONNECTION_ID)
        )
        assert [item["id"] for item in listed.json()] == [sync_id]

        versions = await client.get(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=CONNECTION_ID)
        )
        assert [v["version"] for v in versions.json()] == [1]
        assert versions.json()[0]["sync_id"] == sync_id

        closed = await client.delete(stand.url(CatalogUrl.SYNC, sync_id=sync_id))
        assert closed.status_code == 409

        missing = await client.get(stand.url(CatalogUrl.SYNC, sync_id=uuid4()))
        assert missing.status_code == 404

    async with stand.client(_user(STRANGER_ID, "wrt")) as client:
        refused = await client.post(
            stand.url(CatalogUrl.CONNECTION_SYNCS, connection_id=CONNECTION_ID),
            json={},
        )
        assert refused.status_code == 422
        assert "not visible" in refused.json()["detail"]

    async with stand.client(_user(VIEWER_ID, "read")) as client:
        forbidden = await client.post(
            stand.url(CatalogUrl.CONNECTION_SYNCS, connection_id=CONNECTION_ID),
            json={},
        )
        assert forbidden.status_code == 403

        visible = await client.get(
            stand.url(CatalogUrl.CONNECTION_SYNCS, connection_id=CONNECTION_ID)
        )
        assert visible.status_code == 200


CONNECTIONS_SCHEMA = "catalog_api_connections"
CONNECTION_ROLE = "wrt"


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


@pytest.fixture
async def connections(pool: AsyncPostgresPool) -> BrokerStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(
                sql.Identifier(CONNECTIONS_SCHEMA)
            )
        )

    cfg = ConnectionsConfig(
        enable=True, db_schema=CONNECTIONS_SCHEMA, encryption_key=_key()
    )
    built = BrokerStore(cfg, ConnectionTypes.discover(), pool)
    await built.setup()
    await built.sync_roles([CONNECTION_ROLE])
    return built


@pytest.fixture
async def connections_stand(
    pool: AsyncPostgresPool, studio_config: StudioAppConfig, connections: BrokerStore
) -> Stand:
    """Стенд каталога с общим API соединений под тем же префиксом."""
    catalog = await CatalogStand.build(pool, CONFIG, CatalogStand.kinds())
    # каталог видит подключения тем же брокером, что и общий API
    directory = BrokerConnectionDirectory(UserConnectionsService(lambda: connections))
    service = catalog.service(SyncPorts(NoSyncTools(), directory))
    return Stand(service, ChatProfiles(studio_config.profiles), connections)


def _web_body(name: str, url: str) -> dict[str, object]:
    return {
        "name": name,
        "profile": {"kind": "web", "base_url": url, "ssl_verify": False},
    }


async def test_connections_are_served_next_to_the_catalog(
    connections_stand: Stand, connections: BrokerStore
) -> None:
    """Общий API соединений того же api: схема профилей, общие по роли
    строки видны с маскированными секретами, свои создаются, правятся и
    удаляются, чужие не видны, вход обязателен."""
    stand = connections_stand
    roles = StoredRole.by_name(await connections.roles())
    shared = await connections.add("shared", HttpConnection(base_url="https://a.test"))
    await connections.grant(shared, GrantTarget.role(roles[CONNECTION_ROLE]))
    hidden = await connections.add("hidden", HttpConnection(base_url="https://b.test"))
    await connections.grant(hidden, GrantTarget.user(UUID(int=999_999)))

    async with stand.client(None) as client:
        anonymous = await client.get(Stand.api_url(ConnectionUrl.CONNECTIONS))
        assert anonymous.status_code == 401

    async with stand.client(_user(EDITOR_ID, CONNECTION_ROLE)) as client:
        schema = await client.get(Stand.api_url(ConnectionUrl.SCHEMA))
        assert schema.status_code == 200
        assert "web" in json.dumps(schema.json())

        listed = await client.get(Stand.api_url(ConnectionUrl.CONNECTIONS))
        assert listed.status_code == 200
        assert {row["name"] for row in listed.json()} == {"shared"}
        assert listed.json()[0]["mine"] is False

        created = await client.post(
            Stand.api_url(ConnectionUrl.CONNECTIONS),
            json=_web_body("mine", "https://c.test"),
        )
        assert created.status_code == 200, created.text
        assert created.json()["mine"] is True
        connection_id = created.json()["id"]

        by_kind = await client.get(
            Stand.api_url(ConnectionUrl.CONNECTIONS), params={"kind": "web"}
        )
        assert {row["name"] for row in by_kind.json()} == {"shared", "mine"}

        replaced = await client.put(
            Stand.api_url(ConnectionUrl.CONNECTION, connection_id=connection_id),
            json=_web_body("mine2", "https://c.test"),
        )
        assert replaced.status_code == 200
        assert replaced.json()["name"] == "mine2"

        forbidden = await client.delete(
            Stand.api_url(ConnectionUrl.CONNECTION, connection_id=shared)
        )
        assert forbidden.status_code == 403

        deleted = await client.delete(
            Stand.api_url(ConnectionUrl.CONNECTION, connection_id=connection_id)
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        gone = await client.get(
            Stand.api_url(ConnectionUrl.CONNECTION_CHECK, connection_id=hidden)
        )
        assert gone.status_code in (404, 405)


async def test_held_connection_cannot_be_deleted(
    connections_stand: Stand, studio_config: StudioAppConfig
) -> None:
    """Подключение с версиями снимка удалить нельзя (409) с понятной причиной;
    после «forget versions» — можно. Имя подключения из версии, а не id."""
    stand = connections_stand
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        profile = studio_config.data_layer.postgres.model_dump(mode="json")
        created = await client.post(
            Stand.api_url(ConnectionUrl.CONNECTIONS),
            json={"name": "mine-pg", "profile": profile},
        )
        assert created.status_code == 200, created.text
        connection_id = created.json()["id"]
        assert created.json()["kind"] == "postgres"

        written = await client.post(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=connection_id),
            json={"snapshot": PgSample().snapshot().model_dump(mode="json")},
        )
        assert written.status_code == 200, written.text
        assert written.json()["connection_name"] == "mine-pg"

        held = await client.delete(
            Stand.api_url(ConnectionUrl.CONNECTION, connection_id=connection_id)
        )
        assert held.status_code == 409
        assert "'mine-pg'" in held.json()["detail"]
        assert "has 1 catalog version(s); forget them first" in held.json()["detail"]

        forgotten = await client.delete(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=connection_id)
        )
        assert forgotten.status_code == 200

        deleted = await client.delete(
            Stand.api_url(ConnectionUrl.CONNECTION, connection_id=connection_id)
        )
        assert deleted.status_code == 200
