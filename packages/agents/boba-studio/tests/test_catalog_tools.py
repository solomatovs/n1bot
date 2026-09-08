"""Инструменты каталога studio на живом postgres: тела зовутся напрямую под
контекстом вызова стенда, ответы сверяются моделями результатов.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from boba.catalog import (
    AddGroup,
    AddNode,
    OperationList,
)
from boba.catalog.samples import ProcessSample
from boba.catalog_service import (
    AuthorVia,
    CatalogService,
    ProcessSpec,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot_sample import PgSample
from boba.identity.context import Subject
from boba.stand.catalog_ports import (
    FakeConnections,
    FakeSyncPorts,
    StubSyncPorts,
)
from boba.stand.catalog_stand import CatalogStand
from boba.stand.context import use_context
from boba.studio.catalog.tools import CatalogTools
from boba.toolkit.result import (
    ErrorResult,
    JsonResult,
    TableResult,
    TextResult,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONFIG = CatalogStand.config("catalog_tools_test", ("read",), ("wrt",))
PREFIX = "/boba-test"
PG_CONNECTION = FakeConnections.info("prod-pg", "postgres")
CONNECTION_ID = PG_CONNECTION.id
SPARE_CONNECTION = FakeConnections.info("prod-replica", "postgres")


@pytest.fixture
async def service(pool: AsyncPostgresPool) -> CatalogService:
    stand = await CatalogStand.build(pool, CONFIG, CatalogStand.kinds())
    return stand.service(StubSyncPorts((PG_CONNECTION,)))


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
    """Подключение prod-pg с версией 1 из образца и процесс orders; образец
    процесса ссылается на подключение."""
    await service.write_connection_version(
        editor, PG_CONNECTION.id, PgSample().snapshot()
    )
    await service.create_process(editor, ProcessSpec(name="orders"))
    return ProcessSample(PG_CONNECTION.id)


def _operations(process: ProcessSample) -> str:
    ops = OperationList(
        root=(AddGroup(group=process.raw), AddNode(node=process.orders))
    )
    return ops.model_dump_json()


async def test_read_lists_processes_and_reads_an_empty_one(
    tools: CatalogTools, editor: Subject, service: CatalogService
) -> None:
    _, none = await tools.read("", "")
    assert isinstance(none, TextResult)
    assert "no processes yet" in none.text

    created = await service.create_process(
        editor, ProcessSpec(name="orders", description="sales")
    )
    _, listed = await tools.read("", "")
    assert isinstance(listed, TableResult)
    assert listed.rows[0]["process_id"] == str(created.id)
    assert listed.rows[0]["name"] == "orders"
    assert listed.rows[0]["nodes"] == 0

    _, result = await tools.read("orders", "")
    assert isinstance(result, JsonResult)
    assert result.payload["process"] == "orders"
    assert result.payload["version"] == 0
    assert result.payload["nodes"] == []

    _, by_id = await tools.read(str(created.id), "")
    assert isinstance(by_id, JsonResult)
    assert by_id.payload["process_id"] == str(created.id)

    _, missing = await tools.read("nowhere", "")
    assert isinstance(missing, ErrorResult)
    assert missing.error_kind == "catalog_bad_id"


async def test_draft_propose_diff_open(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    _, listed = await tools.draft("orders", "")
    assert isinstance(listed, TextResult)
    assert "no open drafts" in listed.text

    _, created = await tools.draft("orders", "first")
    assert isinstance(created, TextResult)
    draft_id = created.metadata["draft_id"]
    assert "draft created" in created.text
    assert "process 'orders'" in created.text

    _, table = await tools.draft("orders", " ")
    assert isinstance(table, TableResult)
    assert [row["draft_id"] for row in table.rows] == [draft_id]

    _, proposed = await tools.propose(draft_id, _operations(process))
    assert isinstance(proposed, TextResult)
    assert proposed.metadata["seq"] == "1"
    assert "added group 'raw'" in proposed.text
    assert f"added node '{process.orders.ref.render()}'" in proposed.text

    _, diff = await tools.diff(draft_id)
    assert isinstance(diff, TextResult)
    assert "at seq 1 over version 0: 2 change(s)" in diff.text

    _, rejected = await tools.propose(draft_id, _operations(process))
    assert isinstance(rejected, ErrorResult)
    assert rejected.error_kind == "catalog_operation_rejected"
    assert "operation #0 (add_group) was rejected" in rejected.message

    content, link = await tools.open("draft", draft_id)
    assert isinstance(link, TextResult)
    assert link.metadata["url"] == f"{PREFIX}/catalog/drafts/{draft_id}"
    assert link.metadata["label"] == "first"
    assert f"{PREFIX}/catalog/drafts/{draft_id}" in content


async def test_draft_of_a_new_process_is_listed_and_named_by_the_tool(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    """Пустой process заводит черновик нового процесса; пустое имя без
    процесса перечисляет все свои черновики, с процессом — только его."""
    _, created = await tools.draft("", "refunds")
    assert isinstance(created, TextResult)
    assert "of a new process" in created.text
    draft_id = created.metadata["draft_id"]

    _, mine = await tools.draft("", "")
    assert isinstance(mine, TableResult)
    assert [row["draft_id"] for row in mine.rows] == [draft_id]
    assert mine.rows[0]["process_id"] == ""

    _, of_orders = await tools.draft("orders", "")
    assert isinstance(of_orders, TextResult)
    assert "no open drafts" in of_orders.text

    _, proposed = await tools.propose(draft_id, _operations(process))
    assert isinstance(proposed, TextResult)
    assert "over version 0" in proposed.text


async def test_read_slice_with_neighbours(
    tools: CatalogTools,
    editor: Subject,
    service: CatalogService,
    process: ProcessSample,
) -> None:
    """Срез по узлу orders тянет v_orders по потоку и clients как второго
    соседа v_orders, колонки берутся из привязанной версии источника,
    неизвестная подпись возвращается списком."""
    _, created = await tools.draft("orders", "seed")
    assert isinstance(created, TextResult)
    draft_id = created.metadata["draft_id"]
    await tools.propose(draft_id, process.ops().model_dump_json())

    await service.publish(editor, UUID(draft_id), AuthorVia.USER)

    _, sliced = await tools.read("orders", "orders, missing")
    assert isinstance(sliced, JsonResult)
    assert sliced.payload["version"] == 1
    assert sliced.payload["pins"] == {str(process.connection_id): 1}
    assert {n["label"] for n in sliced.payload["nodes"]} == {
        process.orders.label,
        process.v_orders.label,
        process.customers.label,
    }
    by_label = {n["label"]: n for n in sliced.payload["nodes"]}
    assert by_label[process.orders.label]["group"] == "raw"
    assert by_label[process.orders.label]["position"] == {"x": 0.0, "y": 0.0}
    assert by_label[process.v_orders.label]["group_id"] == str(process.dm.id)
    assert [c["name"] for c in by_label[process.orders.label]["columns"]] == [
        "id",
        "amount",
        "created_at",
    ]
    assert len(sliced.payload["flows"]) == 2
    flows = {f["from_node"]: f for f in sliced.payload["flows"]}
    assert flows[process.orders.label]["columns"] == [
        {"from_column": "id", "to_column": "id"},
        {"from_column": "amount", "to_column": "id"},
    ]
    assert sliced.payload["unknown_nodes"] == ["missing"]


async def test_bad_inputs_are_error_results(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    _, bad_id = await tools.diff("not-a-uuid")
    assert isinstance(bad_id, ErrorResult)
    assert bad_id.error_kind == "catalog_bad_id"

    _, missing = await tools.diff(str(UUID(int=404)))
    assert isinstance(missing, ErrorResult)
    assert missing.error_kind == "catalog_not_found"

    _, created = await tools.draft("orders", "bad ops")
    assert isinstance(created, TextResult)
    bad_ops = '[{"op": "nope"}]'
    _, malformed = await tools.propose(created.metadata["draft_id"], bad_ops)
    assert isinstance(malformed, ErrorResult)
    assert malformed.error_kind == "catalog_bad_operations"

    _, wrong_kind = await tools.open("page", str(UUID(int=1)))
    assert isinstance(wrong_kind, ErrorResult)
    assert wrong_kind.error_kind == "catalog_bad_id"


async def test_process_link_and_role_refusal(
    tools: CatalogTools,
    editor: Subject,
    service: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await service.create_process(editor, ProcessSpec(name="all"))

    _, link = await tools.open("process", str(created.id))
    assert isinstance(link, TextResult)
    assert link.metadata["url"] == f"{PREFIX}/catalog/processes/{created.id}"
    assert link.metadata["label"] == "all"

    use_context(monkeypatch, thread_id="other-thread", user_id=UUID(int=99), roles=())
    _, refused = await tools.read("", "")
    assert isinstance(refused, ErrorResult)
    assert refused.error_kind == "catalog_view_forbidden"


@pytest.fixture
async def sync_service(
    pool: AsyncPostgresPool,
    tmp_path: Path,
    editor: Subject,
    test_postgres: PostgresConfig,
) -> CatalogService:
    """Сервис с фейком снятия, видимым редактору."""
    stand = await CatalogStand.build(pool, CONFIG, CatalogStand.fake_kinds())
    site = stand.fake_site(tmp_path, "wrt", editor.profile, test_postgres)
    ports = FakeSyncPorts(site, (PG_CONNECTION, SPARE_CONNECTION), (editor.user_id,))
    return stand.service(ports)


@pytest.fixture
def sync_tools(sync_service: CatalogService) -> CatalogTools:
    async def source() -> CatalogService:
        return sync_service

    return CatalogTools(source, lambda: PREFIX)


async def test_upgrade_by_name_id_or_everything(
    tools: CatalogTools,
    editor: Subject,
    service: CatalogService,
    process: ProcessSample,
) -> None:
    """Upgrade по имени процесса, по id и всех сразу: без отставания — moved
    без версии, после новой версии снимка с удалённой таблицей — blocked с
    проблемами узла; процесс без версий — отказ с текстом."""
    draft = await service.create_draft(editor, None, "flows")
    await service.append_ops(editor, draft.id, 0, process.ops(), AuthorVia.USER)
    published = await service.publish(editor, draft.id, AuthorVia.USER)

    _, same = await tools.upgrade("flows")
    assert isinstance(same, JsonResult), same
    assert same.payload["run"]["status"] == "done"
    assert same.payload["run"]["target"] == "process"
    assert (same.payload["run"]["moved"], same.payload["run"]["blocked"]) == (1, 0)
    assert same.payload["upgrades"] == []

    _, nothing = await tools.upgrade("")
    assert isinstance(nothing, JsonResult)
    assert nothing.payload["run"]["total"] == 0

    await service.write_connection_version(
        editor, PG_CONNECTION.id, PgSample().next_version()
    )
    _, blocked = await tools.upgrade(str(published.process_id))
    assert isinstance(blocked, JsonResult), blocked
    result = blocked.payload["upgrades"][0]
    assert result["status"] == "blocked"
    reasons = {problem["reason"] for problem in result["problems"]}
    assert reasons == {"object_removed"}

    _, everything = await tools.upgrade("")
    assert isinstance(everything, JsonResult)
    run = everything.payload["run"]
    assert (run["moved"], run["blocked"]) == (0, 1)

    _, empty = await tools.upgrade("orders")
    assert isinstance(empty, ErrorResult)
    assert "no published versions" in empty.message


async def test_sync_by_connection_name_or_id(
    sync_tools: CatalogTools, sync_service: CatalogService, editor: Subject
) -> None:
    """Подключение по имени или id; ответ — запись синхронизации с номером
    версии; сорвавшаяся синхронизация — ErrorResult с причиной; неизвестное
    имя — отказ с текстом."""
    _, done = await sync_tools.sync("prod-pg", "")
    assert isinstance(done, JsonResult), done
    assert done.payload["status"] == "done"
    assert done.payload["version"] == 1
    assert done.payload["connection_name"] == "prod-pg"

    _, failed = await sync_tools.sync(str(CONNECTION_ID), "crash")
    assert isinstance(failed, ErrorResult)
    assert "crashed on purpose" in failed.message

    _, replica = await sync_tools.sync("prod-replica", "")
    assert isinstance(replica, JsonResult), replica
    assert replica.payload["connection_id"] == str(SPARE_CONNECTION.id)

    _, missing = await sync_tools.sync("nowhere", "")
    assert isinstance(missing, ErrorResult)
    assert "expected a uuid id" in missing.message
