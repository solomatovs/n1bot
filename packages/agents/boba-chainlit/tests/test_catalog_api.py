"""JSON API каталога через HTTP на стенде: коды ответов по контракту модуля api."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from chainlit.user import PersistedUser
from chainlit_stand import AppConfig
from fastapi import APIRouter, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from pydantic import SecretStr

from boba.catalog import (
    AddGroup,
    OperationList,
    SourceKinds,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    CatalogConfig,
    CatalogService,
    ConnectionInfo,
    ConnectionStore,
    ProcessStore,
    SyncPorts,
)
from boba.chainlit.catalog.api import CatalogApi, CatalogUrl
from boba.chainlit.catalog.subjects import ChainlitSubjects, SignedIn
from boba.chainlit.catalog.sync_ports import (
    BrokerConnectionDirectory,
    CatalogHoldGuard,
)
from boba.chat.profiles import ChatProfiles
from boba.connection_broker.api import ConnectionsApi, ConnectionUrl
from boba.connection_broker.service import UserConnectionsService
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.store import ConnectionStore as BrokerStore
from boba.connection_broker.tickets import CredentialSource
from boba.connections.manifest import ConnectionTypes
from boba.connections.profile import GrantTarget, StoredRole
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.signin import SignInMetadata
from boba.messaging import MemoryMessageBus
from boba.stand.catalog_ports import FakeSyncPorts, NoSyncTools, StubSyncPorts
from boba.transport.http.profile import HttpConnection

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)
"""Реестр видов теста: оба снимка из пакетов драйверов."""

SCHEMA = "catalog_api_test"
EDITOR_ID = UUID(int=21)
VIEWER_ID = UUID(int=22)
STRANGER_ID = UUID(int=23)
OTHER_EDITOR_ID = UUID(int=24)
CONNECTION_ID = UUID(int=77)
PG_CONNECTION = ConnectionInfo(id=CONNECTION_ID, name="prod-pg", kind="postgres")
CH_CONNECTION = ConnectionInfo(id=UUID(int=78), name="dwh-ch", kind="clickhouse")
ORACLE_CONNECTION = ConnectionInfo(id=UUID(int=79), name="ora", kind="oracle")
STAND_CONNECTIONS = (PG_CONNECTION, CH_CONNECTION, ORACLE_CONNECTION)


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
    """Приложение с маршрутами каталога, общим API соединений под тем же
    префиксом и подменой пользователя входа."""

    def __init__(
        self,
        service: CatalogService,
        profiles: ChatProfiles,
        connections: BrokerStore | None = None,
    ) -> None:
        self.service = service
        self.app = FastAPI()
        router = APIRouter(prefix=CatalogUrl.PREFIX.value)

        async def source() -> CatalogService:
            return service

        async def signed_in(request: Request) -> PersistedUser | None:
            return self.user

        subjects = ChainlitSubjects(profiles, signed_in)
        CatalogApi(source, subjects).mount(router)
        if connections is not None:
            store = connections
            guards = (CatalogHoldGuard(source),)
            ConnectionsApi(
                UserConnectionsService(lambda: store, guards),
                subjects.of_request,
                self._no_credentials,
                lambda: service.bus,
                ConnectionTypes.discover(),
            ).mount(router)

        self.app.include_router(router)
        self.user: PersistedUser | None = None
        self.app.dependency_overrides[SignedIn.user] = lambda: self.user

    @staticmethod
    def _no_credentials() -> CredentialSource:
        msg = "the catalog api stand carries no kerberos credentials"
        raise RuntimeError(msg)

    def client(self, user: PersistedUser | None) -> AsyncClient:
        self.user = user
        transport = ASGITransport(app=self.app)
        return AsyncClient(transport=transport, base_url="http://stand")

    @staticmethod
    def url(path: CatalogUrl, **params: Any) -> str:
        return CatalogUrl.PREFIX.value + path.value.format(**params)


async def _stores(
    pool: AsyncPostgresPool, kinds: SourceKinds
) -> tuple[ProcessStore, ConnectionStore]:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    processes = ProcessStore(_config(), pool)
    await processes.setup()
    connections = ConnectionStore(_config(), kinds, pool)
    await connections.setup()
    return processes, connections


@pytest.fixture
async def stand(pool: AsyncPostgresPool, app_config: AppConfig) -> Stand:
    processes, connections = await _stores(pool, KINDS)
    service = CatalogService(
        processes,
        connections,
        _config(),
        MemoryMessageBus("test:0"),
        StubSyncPorts(STAND_CONNECTIONS),
    )
    return Stand(service, ChatProfiles(app_config.profiles))


class FakeKindSnapshot(PgSnapshot):
    """Снимок вида postgres, чей инструмент снятия — фейк стенда."""

    SYNC_TOOL = "fake_pg_snapshot"


@pytest.fixture
async def sync_stand(
    pool: AsyncPostgresPool, app_config: AppConfig, tmp_path: Path
) -> Stand:
    """Стенд с фейком снятия: роль wrt и профиль по умолчанию видят инструмент."""
    processes, connections = await _stores(
        pool, SourceKinds.of(FakeKindSnapshot, ChSnapshot)
    )
    profiles = ChatProfiles(app_config.profiles)
    ports = FakeSyncPorts(
        tmp_path, "wrt", profiles.default_name(), (PG_CONNECTION,), (EDITOR_ID,)
    )
    service = CatalogService(
        processes, connections, _config(), MemoryMessageBus("test:0"), ports
    )
    return Stand(service, profiles)


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
    async with stand.client(_user(STRANGER_ID)) as client:
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
    поднятие привязок без новых версий ничего не ломает; после публикации
    контекст и устаревание есть у опубликованного процесса."""
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

        bumped = await client.post(stand.url(CatalogUrl.DRAFT_PINS, draft_id=draft_id))
        assert bumped.status_code == 200
        assert bumped.json()["violations"] == []

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


async def test_disabled_service_gives_503(app_config: AppConfig) -> None:
    async def source() -> CatalogService:
        msg = "[catalog] is disabled: the data catalog is unavailable"
        raise RuntimeError(msg)

    app = FastAPI()
    router = APIRouter(prefix=CatalogUrl.PREFIX.value)
    CatalogApi(source, ChainlitSubjects(ChatProfiles(app_config.profiles))).mount(
        router
    )
    app.include_router(router)
    app.dependency_overrides[SignedIn.user] = lambda: _user(EDITOR_ID, "wrt")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://stand"
    ) as client:
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
            json={"schemas": [], "batch_size": 3, "pause_ms": 0},
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
    pool: AsyncPostgresPool, app_config: AppConfig, connections: BrokerStore
) -> Stand:
    """Стенд каталога с общим API соединений под тем же префиксом."""
    processes, snapshots = await _stores(pool, KINDS)
    # каталог видит подключения тем же брокером, что и общий API
    directory = BrokerConnectionDirectory(UserConnectionsService(lambda: connections))
    ports = SyncPorts(NoSyncTools(), directory)
    service = CatalogService(
        processes, snapshots, _config(), MemoryMessageBus("test:0"), ports
    )
    return Stand(service, ChatProfiles(app_config.profiles), connections)


def _web_body(name: str, url: str) -> dict[str, object]:
    return {
        "name": name,
        "profile": {"kind": "web", "base_url": url, "ssl_verify": False},
    }


async def test_connections_are_served_under_the_catalog_prefix(
    connections_stand: Stand, connections: BrokerStore
) -> None:
    """Общий API соединений под /api/catalog: схема профилей, общие по роли
    строки видны с маскированными секретами, свои создаются, правятся и
    удаляются, чужие не видны, вход обязателен."""
    stand = connections_stand
    roles = StoredRole.by_name(await connections.roles())
    shared = await connections.add("shared", HttpConnection(base_url="https://a.test"))
    await connections.grant(shared, GrantTarget.role(roles[CONNECTION_ROLE]))
    hidden = await connections.add("hidden", HttpConnection(base_url="https://b.test"))
    await connections.grant(hidden, GrantTarget.user(UUID(int=999_999)))

    async with stand.client(None) as client:
        anonymous = await client.get(
            CatalogUrl.PREFIX.value + ConnectionUrl.CONNECTIONS
        )
        assert anonymous.status_code == 401

    async with stand.client(_user(EDITOR_ID, CONNECTION_ROLE)) as client:
        schema = await client.get(CatalogUrl.PREFIX.value + ConnectionUrl.SCHEMA)
        assert schema.status_code == 200
        assert "web" in json.dumps(schema.json())

        listed = await client.get(CatalogUrl.PREFIX.value + ConnectionUrl.CONNECTIONS)
        assert listed.status_code == 200
        assert {row["name"] for row in listed.json()} == {"shared"}
        assert listed.json()[0]["mine"] is False

        created = await client.post(
            CatalogUrl.PREFIX.value + ConnectionUrl.CONNECTIONS,
            json=_web_body("mine", "https://c.test"),
        )
        assert created.status_code == 200, created.text
        assert created.json()["mine"] is True
        connection_id = created.json()["id"]

        by_kind = await client.get(
            CatalogUrl.PREFIX.value + ConnectionUrl.CONNECTIONS, params={"kind": "web"}
        )
        assert {row["name"] for row in by_kind.json()} == {"shared", "mine"}

        replaced = await client.put(
            CatalogUrl.PREFIX.value
            + ConnectionUrl.CONNECTION.value.format(connection_id=connection_id),
            json=_web_body("mine2", "https://c.test"),
        )
        assert replaced.status_code == 200
        assert replaced.json()["name"] == "mine2"

        forbidden = await client.delete(
            CatalogUrl.PREFIX.value
            + ConnectionUrl.CONNECTION.value.format(connection_id=shared)
        )
        assert forbidden.status_code == 403

        deleted = await client.delete(
            CatalogUrl.PREFIX.value
            + ConnectionUrl.CONNECTION.value.format(connection_id=connection_id)
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        gone = await client.get(
            CatalogUrl.PREFIX.value
            + ConnectionUrl.CONNECTION_CHECK.value.format(connection_id=hidden)
        )
        assert gone.status_code in (404, 405)


async def test_held_connection_cannot_be_deleted(
    connections_stand: Stand, app_config: AppConfig
) -> None:
    """Подключение с версиями снимка удалить нельзя (409) с понятной причиной;
    после «forget versions» — можно. Имя подключения из версии, а не id."""
    stand = connections_stand
    async with stand.client(_user(EDITOR_ID, "wrt")) as client:
        profile = app_config.data_layer.postgres.model_dump(mode="json")
        created = await client.post(
            CatalogUrl.PREFIX.value + ConnectionUrl.CONNECTIONS,
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
            CatalogUrl.PREFIX.value
            + ConnectionUrl.CONNECTION.value.format(connection_id=connection_id)
        )
        assert held.status_code == 409
        assert "'mine-pg'" in held.json()["detail"]
        assert "has 1 catalog version(s); forget them first" in held.json()["detail"]

        forgotten = await client.delete(
            stand.url(CatalogUrl.CONNECTION_VERSIONS, connection_id=connection_id)
        )
        assert forgotten.status_code == 200

        deleted = await client.delete(
            CatalogUrl.PREFIX.value
            + ConnectionUrl.CONNECTION.value.format(connection_id=connection_id)
        )
        assert deleted.status_code == 200
