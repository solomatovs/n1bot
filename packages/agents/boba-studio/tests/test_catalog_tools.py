"""Инструменты каталога studio на живом postgres: тела зовутся напрямую под
контекстом вызова стенда, ответы сверяются моделями результатов.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
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
    MarkdownResult,
    TableResult,
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


def _payload(result: MarkdownResult) -> Any:
    """JSON-текст результата обратно в структуру для проверок."""
    return json.loads(result.text)


async def test_read_lists_processes_and_reads_an_empty_one(
    tools: CatalogTools, editor: Subject, service: CatalogService
) -> None:
    none = await tools.read("", "")
    assert isinstance(none, MarkdownResult)
    assert "no processes yet" in none.text

    created = await service.create_process(
        editor, ProcessSpec(name="orders", description="sales")
    )
    listed = await tools.read("", "")
    assert isinstance(listed, TableResult)
    assert listed.rows[0]["process_id"] == str(created.id)
    assert listed.rows[0]["name"] == "orders"
    assert listed.rows[0]["nodes"] == 0

    result = await tools.read("orders", "")
    assert isinstance(result, MarkdownResult)

    result_json = _payload(result)
    assert result_json["process"] == "orders"
    assert result_json["version"] == 0
    assert result_json["nodes"] == []

    by_id = await tools.read(str(created.id), "")
    assert isinstance(by_id, MarkdownResult)

    by_id_json = _payload(by_id)
    assert by_id_json["process_id"] == str(created.id)

    missing = await tools.read("nowhere", "")
    assert isinstance(missing, ErrorResult)
    assert missing.error_kind == "catalog_bad_id"


async def test_draft_propose_diff_open(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    listed = await tools.draft("orders", "")
    assert isinstance(listed, MarkdownResult)
    assert "no open drafts" in listed.text

    created = await tools.draft("orders", "first")
    assert isinstance(created, MarkdownResult)
    draft_id = created.metadata["draft_id"]
    assert "draft created" in created.text
    assert "process 'orders'" in created.text

    table = await tools.draft("orders", " ")
    assert isinstance(table, TableResult)
    assert [row["draft_id"] for row in table.rows] == [draft_id]

    proposed = await tools.propose(draft_id, _operations(process))
    assert isinstance(proposed, MarkdownResult)
    assert proposed.metadata["seq"] == "1"
    assert "added group 'raw'" in proposed.text
    assert f"added node '{process.orders.ref.render()}'" in proposed.text

    diff = await tools.diff(draft_id)
    assert isinstance(diff, MarkdownResult)
    assert "at seq 1 over version 0: 2 change(s)" in diff.text

    rejected = await tools.propose(draft_id, _operations(process))
    assert isinstance(rejected, ErrorResult)
    assert rejected.error_kind == "catalog_operation_rejected"
    assert "operation #0 (add_group) was rejected" in rejected.message

    link = await tools.open("draft", draft_id)
    content = link.llm_view()
    assert isinstance(link, MarkdownResult)
    assert link.metadata["url"] == f"{PREFIX}/catalog/drafts/{draft_id}"
    assert link.metadata["label"] == "first"
    assert f"{PREFIX}/catalog/drafts/{draft_id}" in content


async def test_draft_of_a_new_process_is_listed_and_named_by_the_tool(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    """Пустой process заводит черновик нового процесса; пустое имя без
    процесса перечисляет все свои черновики, с процессом — только его."""
    created = await tools.draft("", "refunds")
    assert isinstance(created, MarkdownResult)
    assert "of a new process" in created.text
    draft_id = created.metadata["draft_id"]

    mine = await tools.draft("", "")
    assert isinstance(mine, TableResult)
    assert [row["draft_id"] for row in mine.rows] == [draft_id]
    assert mine.rows[0]["process_id"] == ""

    of_orders = await tools.draft("orders", "")
    assert isinstance(of_orders, MarkdownResult)
    assert "no open drafts" in of_orders.text

    proposed = await tools.propose(draft_id, _operations(process))
    assert isinstance(proposed, MarkdownResult)
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
    created = await tools.draft("orders", "seed")
    assert isinstance(created, MarkdownResult)
    draft_id = created.metadata["draft_id"]
    await tools.propose(draft_id, process.ops().model_dump_json())

    await service.publish(editor, UUID(draft_id), AuthorVia.USER)

    sliced = await tools.read("orders", "orders, missing")
    assert isinstance(sliced, MarkdownResult)

    sliced_json = _payload(sliced)
    assert sliced_json["version"] == 1
    assert sliced_json["pins"] == {str(process.connection_id): 1}
    assert {n["label"] for n in sliced_json["nodes"]} == {
        process.orders.label,
        process.v_orders.label,
        process.customers.label,
    }
    by_label = {n["label"]: n for n in sliced_json["nodes"]}
    assert by_label[process.orders.label]["group"] == "raw"
    assert by_label[process.orders.label]["position"] == {"x": 0.0, "y": 0.0}
    assert by_label[process.v_orders.label]["group_id"] == str(process.dm.id)
    assert [c["name"] for c in by_label[process.orders.label]["columns"]] == [
        "id",
        "amount",
        "created_at",
    ]
    assert len(sliced_json["flows"]) == 2
    flows = {f["from_node"]: f for f in sliced_json["flows"]}
    assert flows[process.orders.label]["columns"] == [
        {"from_column": "id", "to_column": "id"},
        {"from_column": "amount", "to_column": "id"},
    ]
    assert sliced_json["unknown_nodes"] == ["missing"]


async def test_bad_inputs_are_error_results(
    tools: CatalogTools, editor: Subject, process: ProcessSample
) -> None:
    bad_id = await tools.diff("not-a-uuid")
    assert isinstance(bad_id, ErrorResult)
    assert bad_id.error_kind == "catalog_bad_id"

    missing = await tools.diff(str(UUID(int=404)))
    assert isinstance(missing, ErrorResult)
    assert missing.error_kind == "catalog_not_found"

    created = await tools.draft("orders", "bad ops")
    assert isinstance(created, MarkdownResult)
    bad_ops = '[{"op": "nope"}]'
    malformed = await tools.propose(created.metadata["draft_id"], bad_ops)
    assert isinstance(malformed, ErrorResult)
    assert malformed.error_kind == "catalog_bad_operations"

    wrong_kind = await tools.open("page", str(UUID(int=1)))
    assert isinstance(wrong_kind, ErrorResult)
    assert wrong_kind.error_kind == "catalog_bad_id"


async def test_process_link_and_role_refusal(
    tools: CatalogTools,
    editor: Subject,
    service: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await service.create_process(editor, ProcessSpec(name="all"))

    link = await tools.open("process", str(created.id))
    assert isinstance(link, MarkdownResult)
    assert link.metadata["url"] == f"{PREFIX}/catalog/processes/{created.id}"
    assert link.metadata["label"] == "all"

    use_context(monkeypatch, thread_id="other-thread", user_id=UUID(int=99), roles=())
    refused = await tools.read("", "")
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

    same = await tools.upgrade("flows")
    assert isinstance(same, MarkdownResult), same

    same_json = _payload(same)
    assert same_json["run"]["status"] == "done"
    assert same_json["run"]["target"] == "process"
    assert (same_json["run"]["moved"], same_json["run"]["blocked"]) == (1, 0)
    assert same_json["upgrades"] == []

    nothing = await tools.upgrade("")
    assert isinstance(nothing, MarkdownResult)

    nothing_json = _payload(nothing)
    assert nothing_json["run"]["total"] == 0

    await service.write_connection_version(
        editor, PG_CONNECTION.id, PgSample().next_version()
    )
    blocked = await tools.upgrade(str(published.process_id))
    assert isinstance(blocked, MarkdownResult), blocked

    blocked_json = _payload(blocked)
    result = blocked_json["upgrades"][0]
    assert result["status"] == "blocked"
    reasons = {problem["reason"] for problem in result["problems"]}
    assert reasons == {"object_removed"}

    everything = await tools.upgrade("")
    assert isinstance(everything, MarkdownResult)

    everything_json = _payload(everything)
    run = everything_json["run"]
    assert (run["moved"], run["blocked"]) == (0, 1)

    empty = await tools.upgrade("orders")
    assert isinstance(empty, ErrorResult)
    assert "no published versions" in empty.message


async def test_sync_by_connection_name_or_id(
    sync_tools: CatalogTools, sync_service: CatalogService, editor: Subject
) -> None:
    """Подключение по имени или id; ответ — запись синхронизации с номером
    версии; сорвавшаяся синхронизация — ErrorResult с причиной; неизвестное
    имя — отказ с текстом."""
    done = await sync_tools.sync("prod-pg", "")
    assert isinstance(done, MarkdownResult), done

    done_json = _payload(done)
    assert done_json["status"] == "done"
    assert done_json["version"] == 1
    assert done_json["connection_name"] == "prod-pg"

    failed = await sync_tools.sync(str(CONNECTION_ID), "crash")
    assert isinstance(failed, ErrorResult)
    assert "crashed on purpose" in failed.message

    replica = await sync_tools.sync("prod-replica", "")
    assert isinstance(replica, MarkdownResult), replica

    replica_json = _payload(replica)
    assert replica_json["connection_id"] == str(SPARE_CONNECTION.id)

    missing = await sync_tools.sync("nowhere", "")
    assert isinstance(missing, ErrorResult)
    assert "expected a uuid id" in missing.message
