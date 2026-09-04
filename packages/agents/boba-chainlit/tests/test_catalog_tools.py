"""Инструменты каталога на живом postgres: тела зовутся напрямую под контекстом
вызова стенда, ответы сверяются моделями результатов.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    AddLayer,
    AddNode,
    OperationList,
    SourceKinds,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogService,
    CatalogStore,
    SourceSpec,
    SourceStore,
    ViewSpec,
)
from boba.chainlit.catalog.tools import CatalogTools
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.snapshot import PgSnapshot, PgSourceKind
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.context import Subject
from boba.messaging import MemoryMessageBus
from boba.stand.catalog_ports import FakeSyncPorts, StubSyncPorts
from boba.stand.context import use_context
from boba.toolkit.result import (
    CustomElementResult,
    ErrorResult,
    JsonResult,
    TableResult,
    TextResult,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)
"""Реестр видов теста: оба снимка из пакетов драйверов."""

SCHEMA = "catalog_tools_test"
PREFIX = "/boba-test"


def _config() -> CatalogConfig:
    return CatalogConfig(
        enable=True, db_schema=SCHEMA, view_roles=("read",), edit_roles=("wrt",)
    )


@pytest.fixture
async def service(pool: AsyncPostgresPool) -> CatalogService:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    store = CatalogStore(_config(), pool)
    await store.setup()
    sources = SourceStore(_config(), KINDS, pool)
    await sources.setup()
    return CatalogService(
        store, sources, _config(), MemoryMessageBus("test:0"), StubSyncPorts()
    )


@pytest.fixture
def tools(service: CatalogService) -> CatalogTools:
    async def source() -> CatalogService:
        return service

    return CatalogTools(source, lambda: PREFIX)


@pytest.fixture
def editor(monkeypatch: pytest.MonkeyPatch) -> Subject:
    return use_context(monkeypatch, thread_id="catalog-thread", roles=("wrt",)).subject


@pytest.fixture
async def process(service: CatalogService, editor: Subject) -> ProcessSample:
    """Источник prod с версией 1 из образца; процесс ссылается на него."""
    source = await service.create_source(
        editor, SourceSpec(kind=PgSourceKind.POSTGRES, name="prod")
    )
    await service.write_source_version(editor, source.id, PgSample().snapshot())
    return ProcessSample(source.id)


def _operations(process: ProcessSample) -> str:
    ops = OperationList(
        root=(AddLayer(layer=process.raw), AddNode(node=process.orders))
    )
    return ops.model_dump_json()


async def test_read_empty_catalog(tools: CatalogTools, editor: Subject) -> None:
    _, result = await tools.read("")

    assert isinstance(result, JsonResult)
    assert result.payload["version"] == 0
    assert result.payload["nodes"] == []


async def test_draft_propose_diff_open(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    _, listed = await tools.draft("")
    assert isinstance(listed, TextResult)
    assert "no open drafts" in listed.text

    _, created = await tools.draft("first")
    assert isinstance(created, TextResult)
    draft_id = created.metadata["draft_id"]
    assert "draft created" in created.text

    _, table = await tools.draft(" ")
    assert isinstance(table, TableResult)
    assert [row["draft_id"] for row in table.rows] == [draft_id]

    _, proposed = await tools.propose(draft_id, _operations(process))
    assert isinstance(proposed, TextResult)
    assert proposed.metadata["seq"] == "1"
    assert "added layer 'raw'" in proposed.text
    assert f"added node '{process.orders.ref.render()}'" in proposed.text

    _, diff = await tools.diff(draft_id)
    assert isinstance(diff, TextResult)
    assert "at seq 1 over version 0: 2 change(s)" in diff.text

    _, rejected = await tools.propose(draft_id, _operations(process))
    assert isinstance(rejected, ErrorResult)
    assert rejected.error_kind == "catalog_operation_rejected"
    assert "operation #0 (add_layer) was rejected" in rejected.message

    content, link = await tools.open("draft", draft_id)
    assert isinstance(link, CustomElementResult)
    assert link.element == "CatalogLink"
    assert link.props["url"] == f"{PREFIX}/catalog/drafts/{draft_id}"
    assert link.props["label"] == "first"
    assert f"{PREFIX}/catalog/drafts/{draft_id}" in content


async def test_read_slice_with_neighbours(
    tools: CatalogTools,
    editor: Subject,
    service: CatalogService,
    process: ProcessSample,
) -> None:
    """Срез по узлу orders тянет v_orders по потоку и clients как второго
    соседа v_orders, колонки берутся из привязанной версии источника,
    неизвестная подпись возвращается списком."""
    _, created = await tools.draft("seed")
    assert isinstance(created, TextResult)
    draft_id = created.metadata["draft_id"]
    await tools.propose(draft_id, process.ops().model_dump_json())

    await service.publish(editor, UUID(draft_id), AuthorVia.USER)

    _, sliced = await tools.read("orders, missing")
    assert isinstance(sliced, JsonResult)
    assert sliced.payload["version"] == 1
    assert sliced.payload["pins"] == {str(process.source_id): 1}
    assert {n["label"] for n in sliced.payload["nodes"]} == {
        process.orders.label,
        process.v_orders.label,
        process.customers.label,
    }
    by_label = {n["label"]: n for n in sliced.payload["nodes"]}
    assert by_label[process.orders.label]["layer"] == "raw"
    assert [c["name"] for c in by_label[process.orders.label]["columns"]] == [
        "id",
        "amount",
        "created_at",
    ]
    assert len(sliced.payload["flows"]) == 2
    assert sliced.payload["unknown_nodes"] == ["missing"]


async def test_bad_inputs_are_error_results(
    tools: CatalogTools, editor: Subject
) -> None:
    _, bad_id = await tools.diff("not-a-uuid")
    assert isinstance(bad_id, ErrorResult)
    assert bad_id.error_kind == "catalog_bad_id"

    _, missing = await tools.diff(str(UUID(int=404)))
    assert isinstance(missing, ErrorResult)
    assert missing.error_kind == "catalog_not_found"

    _, created = await tools.draft("bad ops")
    assert isinstance(created, TextResult)
    bad_ops = '[{"op": "nope"}]'
    _, malformed = await tools.propose(created.metadata["draft_id"], bad_ops)
    assert isinstance(malformed, ErrorResult)
    assert malformed.error_kind == "catalog_bad_operations"

    _, wrong_kind = await tools.open("page", str(UUID(int=1)))
    assert isinstance(wrong_kind, ErrorResult)
    assert wrong_kind.error_kind == "catalog_bad_id"


async def test_view_link_and_role_refusal(
    tools: CatalogTools,
    editor: Subject,
    service: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = await service.create_view(editor, ViewSpec(name="all"))

    _, link = await tools.open("view", str(view.id))
    assert isinstance(link, CustomElementResult)
    assert link.props["url"] == f"{PREFIX}/catalog/views/{view.id}"

    use_context(monkeypatch, thread_id="other-thread", user_id=UUID(int=99), roles=())
    _, refused = await tools.read("")
    assert isinstance(refused, ErrorResult)
    assert refused.error_kind == "catalog_view_forbidden"


class FakeKindSnapshot(PgSnapshot):
    """Снимок вида postgres, чей инструмент снятия — фейк стенда."""

    SYNC_TOOL = "fake_pg_snapshot"


CONNECTION_ID = UUID(int=77)


@pytest.fixture
async def sync_service(
    pool: AsyncPostgresPool, tmp_path: Path, editor: Subject
) -> CatalogService:
    """Сервис с фейком снятия, видимым редактору."""
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    store = CatalogStore(_config(), pool)
    await store.setup()
    sources = SourceStore(_config(), SourceKinds.of(FakeKindSnapshot, ChSnapshot), pool)
    await sources.setup()
    ports = FakeSyncPorts(
        tmp_path, "wrt", editor.profile, {CONNECTION_ID: "prod-pg"}, (editor.user_id,)
    )
    return CatalogService(store, sources, _config(), MemoryMessageBus("test:0"), ports)


@pytest.fixture
def sync_tools(sync_service: CatalogService) -> CatalogTools:
    async def source() -> CatalogService:
        return sync_service

    return CatalogTools(source, lambda: PREFIX)


async def test_sync_by_source_name(
    sync_tools: CatalogTools, sync_service: CatalogService, editor: Subject
) -> None:
    """Источник по имени, единственное привязанное подключение подставляется,
    ответ — запись синхронизации с номером версии; без привязки — отказ."""
    service = sync_service
    source = await service.create_source(
        editor, SourceSpec(kind=PgSourceKind.POSTGRES, name="prod")
    )

    _, refused = await sync_tools.sync("prod", "", "")
    assert isinstance(refused, ErrorResult)
    assert "0 bound connection(s)" in refused.message

    await service.bind_connection(editor, source.id, CONNECTION_ID)

    _, done = await sync_tools.sync("prod", "", "")
    assert isinstance(done, JsonResult), done
    assert done.payload["status"] == "done"
    assert done.payload["version"] == 1
    assert done.payload["source_name"] == "prod"

    _, failed = await sync_tools.sync(str(source.id), str(CONNECTION_ID), "crash")
    assert isinstance(failed, ErrorResult)
    assert "crashed on purpose" in failed.message

    _, missing = await sync_tools.sync("nowhere", "", "")
    assert isinstance(missing, ErrorResult)
    assert "expected a uuid id" in missing.message
