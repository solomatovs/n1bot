"""Инструменты каталога на живом postgres: тела зовутся напрямую под контекстом
вызова стенда, ответы сверяются моделями результатов.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog_service import (
    AuthorVia,
    CatalogConfig,
    CatalogService,
    CatalogStore,
    ViewSpec,
)
from boba.chainlit.catalog.tools import CatalogTools
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Subject
from boba.messaging import MemoryMessageBus
from boba.stand.context import use_context
from boba.toolkit.result import (
    CustomElementResult,
    ErrorResult,
    JsonResult,
    TableResult,
    TextResult,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "catalog_tools_test"
PREFIX = "/boba-test"
LAYER_ID = UUID(int=301)
DATASET_ID = UUID(int=310)
COLUMN_ID = UUID(int=320)


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
    return CatalogService(store, _config(), MemoryMessageBus("test:0"))


@pytest.fixture
def tools(service: CatalogService) -> CatalogTools:
    async def source() -> CatalogService:
        return service

    return CatalogTools(source, lambda: PREFIX)


@pytest.fixture
def editor(monkeypatch: pytest.MonkeyPatch) -> Subject:
    return use_context(monkeypatch, thread_id="catalog-thread", roles=("wrt",)).subject


def _operations() -> str:
    ops = [
        {"op": "add_layer", "layer": {"id": str(LAYER_ID), "name": "raw"}},
        {
            "op": "add_dataset",
            "dataset": {
                "id": str(DATASET_ID),
                "layer_id": str(LAYER_ID),
                "name": "orders",
            },
        },
        {
            "op": "add_column",
            "column": {
                "id": str(COLUMN_ID),
                "dataset_id": str(DATASET_ID),
                "name": "order_id",
                "type": "int",
                "nullable": False,
                "is_key": True,
                "position": 0,
            },
        },
    ]
    return json.dumps(ops)


async def test_read_empty_catalog(tools: CatalogTools, editor: Subject) -> None:
    _, result = await tools.read("")

    assert isinstance(result, JsonResult)
    assert result.payload["version"] == 0
    assert result.payload["datasets"] == []


async def test_draft_propose_diff_open(tools: CatalogTools, editor: Subject) -> None:
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

    _, proposed = await tools.propose(draft_id, _operations())
    assert isinstance(proposed, TextResult)
    assert proposed.metadata["seq"] == "1"
    assert "added layer 'raw'" in proposed.text
    assert "added dataset 'orders'" in proposed.text

    _, diff = await tools.diff(draft_id)
    assert isinstance(diff, TextResult)
    assert "at seq 1 over version 0: 3 change(s)" in diff.text

    _, rejected = await tools.propose(draft_id, _operations())
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
    tools: CatalogTools, editor: Subject, service: CatalogService
) -> None:
    _, created = await tools.draft("seed")
    assert isinstance(created, TextResult)
    draft_id = created.metadata["draft_id"]
    await tools.propose(draft_id, _operations())

    await service.publish(editor, UUID(draft_id), AuthorVia.USER)

    _, sliced = await tools.read("orders, missing")
    assert isinstance(sliced, JsonResult)
    assert sliced.payload["version"] == 1
    assert [d["name"] for d in sliced.payload["datasets"]] == ["orders"]
    assert sliced.payload["datasets"][0]["layer"] == "raw"
    assert [c["name"] for c in sliced.payload["datasets"][0]["columns"]] == ["order_id"]
    assert sliced.payload["unknown_datasets"] == ["missing"]


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
