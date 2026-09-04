"""Правки черновика на странице каталога: слой и набор через подсказку имени,
форма набора и редактор колонок, поток из панели и соединением на холсте,
удаление, чужие порции и новые версии по событиям, публикация и
перебазирование устаревшего черновика.

Модуль сеет свой каталог с префиксом ed_ и на выходе публикует его удаление,
чтобы опубликованный каталог стенда остался таким, каким его ждут соседние
модули.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
from playwright.sync_api import Browser, FloatRect, Locator, Page, ViewportSize, expect
from test_catalog_look_ui import EDGE_LABEL, LANE, NODE, READY, _client, _ok

from boba.stand.ui.look import Css
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
EDITABLE = '[data-testid="catalog-page"][data-editable="true"]'
LIVE_TIMEOUT_MS = 15_000


class Ed(StrEnum):
    """Имена посеянных сущностей: всё с префиксом ed_, по нему же и убирается."""

    PREFIX = "ed_"
    SRC = "ed_src"
    DST = "ed_dst"
    ORDERS = "ed_orders"
    SALES = "ed_sales"
    RETURNS = "ed_returns"
    FULL = "ed_full"
    HASH = "ed_hash"
    HASH_FIELD = "hash_columns"


class Seed:
    """Каталог модуля: два слоя, три набора, два вида загрузки, один поток."""

    ID_BASE: ClassVar[int] = 0xE000
    DATASETS: ClassVar[dict[str, str]] = {
        Ed.ORDERS: Ed.SRC,
        Ed.SALES: Ed.DST,
        Ed.RETURNS: Ed.DST,
    }
    COLUMNS: ClassVar[tuple[str, ...]] = ("id", "name")

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def id_of(self, name: str) -> str:
        if name not in self.ids:
            self.ids[name] = str(UUID(int=len(self.ids) + self.ID_BASE))

        return self.ids[name]

    def operations(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for layer in (Ed.SRC, Ed.DST):
            ops.append(
                {"op": "add_layer", "layer": {"id": self.id_of(layer), "name": layer}}
            )

        for dataset, layer in self.DATASETS.items():
            ops.append(
                {
                    "op": "add_dataset",
                    "dataset": {
                        "id": self.id_of(dataset),
                        "layer_id": self.id_of(layer),
                        "name": dataset,
                    },
                }
            )
            for position, column in enumerate(self.COLUMNS):
                ops.append(
                    {
                        "op": "add_column",
                        "column": {
                            "id": self.id_of(f"{dataset}.{column}"),
                            "dataset_id": self.id_of(dataset),
                            "name": column,
                            "type": "text",
                            "nullable": position > 0,
                            "is_key": position == 0,
                            "position": position,
                        },
                    }
                )

        ops.append(
            {
                "op": "add_load_kind",
                "load_kind": {"id": self.id_of(Ed.FULL), "name": Ed.FULL, "fields": []},
            }
        )
        ops.append(
            {
                "op": "add_load_kind",
                "load_kind": {
                    "id": self.id_of(Ed.HASH),
                    "name": Ed.HASH,
                    "fields": [
                        {"name": Ed.HASH_FIELD, "type": "columns", "required": True}
                    ],
                },
            }
        )
        ops.append(
            {
                "op": "add_flow",
                "flow": {
                    "id": self.id_of("orders->sales"),
                    "from_dataset_id": self.id_of(Ed.ORDERS),
                    "to_dataset_id": self.id_of(Ed.SALES),
                    "load": {"kind_id": self.id_of(Ed.FULL), "values": {}},
                },
            }
        )
        return ops


class Cleanup:
    """Операции удаления всего с префиксом ed_ из опубликованного снимка."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot

    def _mine(self, table: str) -> Iterator[dict[str, Any]]:
        for entity in self.snapshot[table].values():
            if str(entity["name"]).startswith(Ed.PREFIX):
                yield entity

    def operations(self) -> list[dict[str, Any]]:
        dataset_ids = {entity["id"] for entity in self._mine("datasets")}
        ops: list[dict[str, Any]] = []
        for flow in self.snapshot["flows"].values():
            touches = flow["from_dataset_id"] in dataset_ids
            if flow["to_dataset_id"] in dataset_ids:
                touches = True

            if touches:
                ops.append({"op": "remove_flow", "id": flow["id"]})

        for dataset_id in dataset_ids:
            ops.append({"op": "remove_dataset", "id": dataset_id})

        for layer in self._mine("layers"):
            ops.append({"op": "remove_layer", "id": layer["id"]})

        for kind in self._mine("load_kinds"):
            ops.append({"op": "remove_load_kind", "id": kind["id"]})

        return ops


class Api:
    """Ходы в JSON API стенда от имени администратора."""

    def __init__(self, admin: httpx.Client) -> None:
        self.admin = admin

    def new_draft(self, name: str) -> str:
        draft = _ok(self.admin.post("/api/catalog/drafts", json={"name": name}))
        return str(draft["id"])

    def state(self, draft_id: str) -> dict[str, Any]:
        return _ok(self.admin.get(f"/api/catalog/drafts/{draft_id}"))

    def append(self, draft_id: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        seq = self.state(draft_id)["seq"]
        return _ok(
            self.admin.post(
                f"/api/catalog/drafts/{draft_id}/ops",
                json={"expected_seq": seq, "operations": ops},
            )
        )

    def publish(self, draft_id: str) -> int:
        version = _ok(self.admin.post(f"/api/catalog/drafts/{draft_id}/publish"))
        return int(version["number"])

    def discard(self, draft_id: str) -> None:
        response = self.admin.delete(f"/api/catalog/drafts/{draft_id}")
        if response.status_code not in (200, 404, 409):
            raise RuntimeError(f"discard failed: {response.status_code}")

    def snapshot(self) -> dict[str, Any]:
        return _ok(self.admin.get("/api/catalog/snapshot"))

    def publish_ops(self, name: str, ops: list[dict[str, Any]]) -> int:
        draft_id = self.new_draft(name)
        self.append(draft_id, ops)
        return self.publish(draft_id)

    def dataset_names(self) -> set[str]:
        names: set[str] = set()
        for dataset in self.snapshot()["datasets"].values():
            names.add(str(dataset["name"]))

        return names


@pytest.fixture(scope="module")
def api(stand: StandProcess) -> Iterator[Api]:
    with _client(stand, "admin") as admin:
        yield Api(admin)


@pytest.fixture(scope="module")
def seeded(api: Api) -> Iterator[Seed]:
    """Каталог модуля публикуется на входе и удаляется публикацией на выходе."""
    seed = Seed()
    api.publish_ops("edit seed", seed.operations())
    try:
        yield seed
    finally:
        cleanup = Cleanup(api.snapshot()).operations()
        if cleanup:
            api.publish_ops("edit cleanup", cleanup)


@pytest.fixture
def draft_id(api: Api, seeded: Seed, request: pytest.FixtureRequest) -> Iterator[str]:
    created = api.new_draft(f"edit {request.node.name}")
    try:
        yield created
    finally:
        api.discard(created)


@pytest.fixture
def page(
    browser: Browser, stand: StandProcess, auth_cookies: list[Any]
) -> Iterator[Page]:
    context = browser.new_context(viewport=WIDE)
    context.add_cookies(auth_cookies)
    opened = context.new_page()
    try:
        yield opened
    finally:
        context.close()


def _open_draft(page: Page, stand: StandProcess, draft_id: str) -> None:
    page.goto(f"{stand.config.base_url}/catalog/drafts/{draft_id}")
    page.wait_for_selector(READY, timeout=30_000)
    page.wait_for_selector(EDITABLE, timeout=30_000)
    page.wait_for_selector(NODE, timeout=30_000)


def _node(page: Page, name: str) -> Locator:
    return page.locator(f'{NODE}[data-dataset="{name}"]')


def _dialog(page: Page, mark: str) -> Locator:
    return page.locator(f'[data-dialog="{mark}"]')


def _prompt_name(page: Page, mark: str, name: str) -> None:
    dialog = _dialog(page, mark)
    expect(dialog).to_be_visible()
    dialog.get_by_role("textbox").fill(name)
    dialog.get_by_role("button", name="save").click()
    expect(dialog).to_have_count(0)


def _centre(box: FloatRect) -> tuple[float, float]:
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _landed(page: Page, seq: int) -> None:
    """Порция с этим номером принята сервером и отражена страницей."""
    expect(page.get_by_test_id("catalog-page")).to_have_attribute(
        "data-seq", str(seq), timeout=LIVE_TIMEOUT_MS
    )


def _snapshot_names(state: dict[str, Any], table: str) -> set[str]:
    names: set[str] = set()
    for entity in state["snapshot"][table].values():
        names.add(str(entity["name"]))

    return names


class TestPrompts:
    def test_add_layer_then_dataset_into_it(
        self, page: Page, stand: StandProcess, api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)

        page.get_by_role("button", name="layer", exact=True).click()
        _prompt_name(page, "layer-name", "ed_new")
        group = page.locator('.pane__group[data-layer="ed_new"]')
        expect(group).to_be_visible()

        group.get_by_role("button", name="add dataset to ed_new").click()
        _prompt_name(page, "dataset-name", "ed_events")
        added = _node(page, "ed_events")
        expect(added).to_be_visible()
        expect(added).to_have_attribute("data-status", "added")
        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-dataset", "ed_events"
        )

        state = api.state(draft_id)
        assert state["seq"] == 2, state["seq"]
        assert "ed_new" in _snapshot_names(state, "layers")
        assert "ed_events" in _snapshot_names(state, "datasets")

    def test_rename_and_remove_empty_layer(
        self, page: Page, stand: StandProcess, api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)

        page.get_by_role("button", name="layer", exact=True).click()
        _prompt_name(page, "layer-name", "ed_tmp")
        page.get_by_role("button", name="rename layer ed_tmp").click()
        _prompt_name(page, "layer-name", "ed_tmp2")
        expect(page.locator('.pane__group[data-layer="ed_tmp2"]')).to_be_visible()

        page.get_by_role("button", name="remove layer ed_tmp2").click()
        expect(page.locator('.pane__group[data-layer="ed_tmp2"]')).to_have_count(0)

        names = _snapshot_names(api.state(draft_id), "layers")
        assert "ed_tmp" not in names
        assert "ed_tmp2" not in names


class TestLanes:
    def test_isolated_dataset_stays_in_its_layer_lane(
        self, page: Page, stand: StandProcess, draft_id: str
    ) -> None:
        """Набор без потоков (ed_returns) не выпадает в отдельную компоненту:
        дорожки слоёв не пересекаются, каждая карточка лежит в своей."""
        _open_draft(page, stand, draft_id)

        src = Css.box(page.locator(f'{LANE}[data-layer="{Ed.SRC}"]'))
        dst = Css.box(page.locator(f'{LANE}[data-layer="{Ed.DST}"]'))
        assert src.right <= dst.x + 1, f"lanes overlap: {src} vs {dst}"

        for dataset, layer in Seed.DATASETS.items():
            node = Css.box(_node(page, dataset))
            lane = Css.box(page.locator(f'{LANE}[data-layer="{layer}"]'))
            assert lane.contains(node, slack=2), f"{dataset} is outside {layer}"


class TestDatasetPanel:
    def test_dataset_form_changes_name_and_owner(
        self, page: Page, stand: StandProcess, api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        _node(page, Ed.ORDERS).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_role("button", name="edit dataset").click()

        form = page.get_by_test_id("dataset-form")
        form.get_by_label("dataset name").fill("ed_orders_v2")
        form.get_by_label("dataset owner").fill("dwh team")
        form.get_by_role("button", name="save").click()

        expect(_node(page, "ed_orders_v2")).to_be_visible()
        expect(_node(page, "ed_orders_v2")).to_have_attribute("data-status", "modified")
        expect(panel).to_have_attribute("data-dataset", "ed_orders_v2")
        expect(panel.locator(".detail__facts")).to_contain_text("dwh team")

        assert "ed_orders_v2" in _snapshot_names(api.state(draft_id), "datasets")

    def test_columns_editor_adds_and_removes_columns(
        self, page: Page, stand: StandProcess, api: Api, seeded: Seed, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        _node(page, Ed.RETURNS).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_role("button", name="edit columns").click()

        editor = page.get_by_test_id("columns-editor")
        editor.get_by_role("button", name="remove column name").click()
        editor.get_by_role("button", name="column", exact=True).click()
        editor.get_by_label("column name").last.fill("amount")
        editor.get_by_label("column type").last.fill("numeric")
        editor.get_by_role("button", name="save columns").click()

        _landed(page, 1)
        rows = panel.get_by_test_id("detail-columns").locator("tbody tr")
        expect(rows).to_have_count(len(Seed.COLUMNS))
        expect(rows.filter(has_text="amount")).to_have_count(1)
        expect(rows.filter(has_text="name")).to_have_count(0)

        page.get_by_role("tab", name="all fields").click()
        expect(_node(page, Ed.RETURNS).locator(".ds-node__column")).to_have_count(
            len(Seed.COLUMNS)
        )

        columns = api.state(draft_id)["snapshot"]["columns"]
        mine = [
            column
            for column in columns.values()
            if column["dataset_id"] == seeded.id_of(Ed.RETURNS)
        ]
        assert sorted(column["name"] for column in mine) == ["amount", "id"]

    def test_remove_dataset_takes_its_flows_along(
        self, page: Page, stand: StandProcess, api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        expect(page.locator(EDGE_LABEL)).to_have_count(1)

        _node(page, Ed.SALES).click()
        page.get_by_test_id("detail-panel").get_by_role(
            "button", name="remove dataset"
        ).click()

        expect(_node(page, Ed.SALES)).to_have_count(0)
        expect(page.locator(EDGE_LABEL)).to_have_count(0)
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)

        state = api.state(draft_id)
        assert Ed.SALES not in _snapshot_names(state, "datasets")
        assert state["snapshot"]["flows"] == {}


class TestFlows:
    def test_flow_from_panel_then_removed_from_its_form(
        self, page: Page, stand: StandProcess, api: Api, seeded: Seed, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        _node(page, Ed.ORDERS).click()
        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name="flow", exact=True
        ).click()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        form.get_by_label("flow target").select_option(label=Ed.RETURNS)
        form.get_by_label("load kind").select_option(label=Ed.HASH)
        form.get_by_label(f"load field {Ed.HASH_FIELD}").select_option(
            value=seeded.id_of(f"{Ed.ORDERS}.id")
        )
        form.get_by_role("button", name="save flow").click()

        expect(form).to_have_count(0)
        expect(page.locator(EDGE_LABEL)).to_have_count(2)
        expect(page.locator(EDGE_LABEL).filter(has_text=Ed.HASH)).to_have_count(1)

        flows = api.state(draft_id)["snapshot"]["flows"]
        assert len(flows) == 2
        hash_kind = seeded.id_of(Ed.HASH)
        hashed = [
            flow for flow in flows.values() if flow["load"]["kind_id"] == hash_kind
        ]
        assert hashed[0]["load"]["values"] == {
            Ed.HASH_FIELD: [seeded.id_of(f"{Ed.ORDERS}.id")]
        }

        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name=f"edit flow to {Ed.RETURNS}"
        ).click()
        page.get_by_test_id("flow-form").get_by_role(
            "button", name="remove flow"
        ).click()
        expect(page.locator(EDGE_LABEL)).to_have_count(1)
        assert len(api.state(draft_id)["snapshot"]["flows"]) == 1

    def test_connecting_nodes_on_the_canvas_opens_the_flow_form(
        self, page: Page, stand: StandProcess, api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        source = _node(page, Ed.ORDERS).locator(".react-flow__handle.source")
        target = _node(page, Ed.RETURNS).locator(".react-flow__handle.target")

        start = source.bounding_box()
        end = target.bounding_box()
        assert start is not None
        assert end is not None

        page.mouse.move(*_centre(start))
        page.mouse.down()
        page.mouse.move(*_centre(end), steps=12)
        page.mouse.up()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        expect(form.locator(".form__note")).to_have_text(f"{Ed.ORDERS} → {Ed.RETURNS}")
        form.get_by_label("load kind").select_option(label=Ed.FULL)
        form.get_by_role("button", name="save flow").click()

        expect(page.locator(EDGE_LABEL)).to_have_count(2)
        assert len(api.state(draft_id)["snapshot"]["flows"]) == 2

    def test_clicking_an_edge_edits_its_flow(
        self, page: Page, stand: StandProcess, api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        page.locator(EDGE_LABEL).first.click()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        form.get_by_label("flow description").fill("nightly full copy")
        form.get_by_role("button", name="save flow").click()
        expect(form).to_have_count(0)
        _landed(page, 1)

        flows = list(api.state(draft_id)["snapshot"]["flows"].values())
        assert flows[0]["description"] == "nightly full copy"


class TestLive:
    def test_foreign_portion_shows_up_and_own_edit_lands_on_top(
        self, page: Page, stand: StandProcess, api: Api, seeded: Seed, draft_id: str
    ) -> None:
        """Порция, добавленная мимо страницы, появляется без перезагрузки; своя
        правка после неё ложится поверх, а не затирает."""
        _open_draft(page, stand, draft_id)

        api.append(
            draft_id,
            [
                {
                    "op": "add_dataset",
                    "dataset": {
                        "id": str(UUID(int=0xE0F0)),
                        "layer_id": seeded.id_of(Ed.DST),
                        "name": "ed_live",
                    },
                }
            ],
        )
        expect(_node(page, "ed_live")).to_be_visible(timeout=LIVE_TIMEOUT_MS)

        _node(page, Ed.SALES).click()
        page.get_by_test_id("detail-panel").get_by_role(
            "button", name="edit dataset"
        ).click()
        form = page.get_by_test_id("dataset-form")
        form.get_by_label("dataset name").fill("ed_sales_v2")
        form.get_by_role("button", name="save").click()
        expect(_node(page, "ed_sales_v2")).to_be_visible()

        state = api.state(draft_id)
        assert state["seq"] == 2
        names = _snapshot_names(state, "datasets")
        assert {"ed_live", "ed_sales_v2"} <= names

    def test_publish_conflict_offers_rebase_then_publishes(
        self, page: Page, stand: StandProcess, api: Api, seeded: Seed, draft_id: str
    ) -> None:
        """Пока черновик открыт, публикуется другой: страница показывает кнопку
        обновления, публикация упирается в конфликт, перебазирование снимает его."""
        _open_draft(page, stand, draft_id)
        page.locator('.pane__group[data-layer="ed_dst"]').get_by_role(
            "button", name="add dataset to ed_dst"
        ).click()
        _prompt_name(page, "dataset-name", "ed_mine")
        expect(page.get_by_test_id("rebase-button")).to_have_count(0)

        version = api.publish_ops(
            "edit other",
            [
                {
                    "op": "add_layer",
                    "layer": {"id": str(UUID(int=0xE0F1)), "name": "ed_other"},
                }
            ],
        )
        rebase = page.get_by_test_id("rebase-button")
        expect(rebase).to_have_text(f"update to v{version}", timeout=LIVE_TIMEOUT_MS)

        page.get_by_test_id("publish-button").click()
        conflict = _dialog(page, "publish-conflict")
        expect(conflict).to_be_visible()
        expect(conflict).to_contain_text(f"published catalog is at v{version}")
        conflict.get_by_role("button", name="update the draft").click()
        expect(conflict).to_have_count(0)
        expect(rebase).to_have_count(0)
        expect(page.locator('.pane__group[data-layer="ed_other"]')).to_be_visible()
        expect(_node(page, "ed_mine")).to_be_visible()

        page.get_by_test_id("publish-button").click()
        expect(page.locator('[data-notice="draft-closed"]')).to_be_visible()
        expect(page.locator('[data-testid="catalog-page"]')).to_have_attribute(
            "data-editable", "false"
        )

        published = api.dataset_names()
        assert "ed_mine" in published
        assert api.state(draft_id)["draft"]["status"] == "published"
