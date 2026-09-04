"""Внешний вид страницы каталога: дорожки слоёв слева направо, карточки наборов,
рёбра потоков, список, тулбар, панель деталей, режимы показа, diff черновика,
узкий экран. Каталог сеется через JSON API живого стенда.

Ожидания цветов — из tokens.css сборки страницы (Tokens); геометрия — из
bounding box узлов и дорожек.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
from catalog_ui import api_client, ok
from playwright.sync_api import Browser, Page, ViewportSize, expect

from boba.stand.ui.look import Css, Tokens, no_horizontal_scroll
from boba.stand.ui.stand import REPO_ROOT, StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}

TOKENS_CSS = (
    REPO_ROOT / "packages/agents/boba-chainlit/web/catalog/src/styles/tokens.css"
)

READY = '[data-testid="canvas"][data-ready="true"]'
NODE = '[data-testid="dataset-node"]'
LANE = '[data-testid="layer-lane"]'
EDGE_LABEL = '[data-testid="flow-edge-label"]'


class Probe:
    """Каталог стенда: три слоя, пять наборов, два вида загрузки, три потока."""

    LAYERS: ClassVar[tuple[str, ...]] = ("look_raw", "look_stg", "look_dm")
    DATASETS: ClassVar[dict[str, str]] = {
        "orders_raw": "look_raw",
        "customers_raw": "look_raw",
        "orders_stg": "look_stg",
        "customers_stg": "look_stg",
        "sales_dm": "look_dm",
    }
    FLOWS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("orders_raw", "orders_stg", "hashkey"),
        ("customers_raw", "customers_stg", "full"),
        ("orders_stg", "sales_dm", "full"),
    )
    KEY_COLUMNS: ClassVar[int] = 1
    ALL_COLUMNS: ClassVar[int] = 3
    DRAFT_DATASET: ClassVar[str] = "returns_raw"

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def id_of(self, name: str) -> str:
        if name not in self.ids:
            self.ids[name] = str(UUID(int=len(self.ids) + 0xA000))

        return self.ids[name]

    def operations(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for layer in self.LAYERS:
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
            for position, column in enumerate(("id", "name", "updated_at")):
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
                "load_kind": {"id": self.id_of("full"), "name": "full", "fields": []},
            }
        )
        ops.append(
            {
                "op": "add_load_kind",
                "load_kind": {
                    "id": self.id_of("hashkey"),
                    "name": "hashkey",
                    "fields": [
                        {"name": "hash_columns", "type": "columns", "required": True}
                    ],
                },
            }
        )

        for source, target, kind in self.FLOWS:
            values: dict[str, Any] = {}
            if kind == "hashkey":
                values = {"hash_columns": [self.id_of(f"{source}.id")]}

            ops.append(
                {
                    "op": "add_flow",
                    "flow": {
                        "id": self.id_of(f"{source}->{target}"),
                        "from_dataset_id": self.id_of(source),
                        "to_dataset_id": self.id_of(target),
                        "load": {"kind_id": self.id_of(kind), "values": values},
                    },
                }
            )

        return ops

    def draft_operations(self) -> list[dict[str, Any]]:
        return [
            {
                "op": "add_dataset",
                "dataset": {
                    "id": self.id_of(self.DRAFT_DATASET),
                    "layer_id": self.id_of("look_raw"),
                    "name": self.DRAFT_DATASET,
                },
            }
        ]


@dataclass(frozen=True)
class Seeded:
    """Что посеяно: вид на весь каталог и черновик с добавленным набором."""

    probe: Probe
    view_id: str
    draft_id: str


@pytest.fixture(scope="module")
def seeded(stand: StandProcess) -> Seeded:
    """Каталог, вид и черновик через API: публикуется ровно один раз на модуль."""
    probe = Probe()
    with api_client(stand, "admin") as admin:
        draft = ok(admin.post("/api/catalog/drafts", json={"name": "look seed"}))
        ok(
            admin.post(
                f"/api/catalog/drafts/{draft['id']}/ops",
                json={"expected_seq": 0, "operations": probe.operations()},
            )
        )
        ok(admin.post(f"/api/catalog/drafts/{draft['id']}/publish"))

        view = ok(
            admin.post(
                "/api/catalog/views",
                json={"name": "look view", "dataset_ids": [], "layer_ids": []},
            )
        )

        edits = ok(admin.post("/api/catalog/drafts", json={"name": "look edits"}))
        ok(
            admin.post(
                f"/api/catalog/drafts/{edits['id']}/ops",
                json={"expected_seq": 0, "operations": probe.draft_operations()},
            )
        )

    return Seeded(probe=probe, view_id=str(view["id"]), draft_id=str(edits["id"]))


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load(TOKENS_CSS)


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


def _relayout(page: Page, action: Callable[[], None]) -> None:
    """Действие меняет раскладку: ждём следующую раскладку, а не прежнюю готовность."""
    canvas = page.get_by_test_id("canvas")
    before = canvas.get_attribute("data-layouts") or "0"
    action()
    expect(canvas).not_to_have_attribute("data-layouts", before, timeout=30_000)
    page.wait_for_selector(READY, timeout=30_000)


def _switch_mode(page: Page, mode: str) -> None:
    """Режим карточек через тулбар с ожиданием новой раскладки."""

    def click() -> None:
        page.get_by_role("tab", name=mode).click()

    _relayout(page, click)


def _open_view(
    page: Page, stand: StandProcess, seeded: Seeded, query: str = ""
) -> None:
    page.goto(f"{stand.config.base_url}/catalog/views/{seeded.view_id}{query}")
    page.wait_for_selector(READY, timeout=30_000)
    page.wait_for_selector(NODE, timeout=30_000)


def _open_draft(
    page: Page, stand: StandProcess, seeded: Seeded, query: str = ""
) -> None:
    page.goto(f"{stand.config.base_url}/catalog/drafts/{seeded.draft_id}{query}")
    page.wait_for_selector(READY, timeout=30_000)
    page.wait_for_selector(NODE, timeout=30_000)


class TestViewPage:
    def test_nodes_edges_and_lanes_are_rendered(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        _open_view(page, stand, seeded)

        expect(page.get_by_test_id("page-title")).to_have_text("look view")
        expect(page.locator(NODE)).to_have_count(len(Probe.DATASETS))
        expect(page.locator(LANE)).to_have_count(len(Probe.LAYERS))
        expect(page.locator(EDGE_LABEL)).to_have_count(len(Probe.FLOWS))

        labels = sorted(page.locator(EDGE_LABEL).all_inner_texts())
        assert labels == ["full", "full", "hashkey"]

    def test_lanes_follow_layer_order_left_to_right(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Партиции ELK: слой-источник левее приёмника, дорожки не пересекаются."""
        _open_view(page, stand, seeded)

        lefts: list[float] = []
        for layer in Probe.LAYERS:
            lane = page.locator(f'{LANE}[data-layer="{layer}"]')
            expect(lane).to_have_count(1)
            lefts.append(Css.box(lane).x)

        assert lefts == sorted(lefts), f"lanes are not ordered by layer: {lefts}"

        boxes = [
            Css.box(page.locator(f'{LANE}[data-layer="{layer}"]'))
            for layer in Probe.LAYERS
        ]
        for previous, current in pairwise(boxes):
            assert previous.right <= current.x + 1, "lanes overlap"

        for dataset, layer in Probe.DATASETS.items():
            node = page.locator(f'{NODE}[data-dataset="{dataset}"]')
            lane = page.locator(f'{LANE}[data-layer="{layer}"]')
            assert Css.box(lane).contains(Css.box(node), slack=2), (
                f"{dataset} is outside its lane {layer}"
            )

    def test_nodes_are_laid_out_by_measured_size_without_overlap(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Раскладка идёт по замеру DOM: карточки не пересекаются в любом режиме,
        а карточка со всеми колонками выше карточки с одними ключами."""
        _open_view(page, stand, seeded)
        node = page.locator(f'{NODE}[data-dataset="orders_raw"]')
        keys_only = Css.box(node).height

        for mode in ("all fields", "names"):
            _switch_mode(page, mode)
            boxes = [
                Css.box(page.locator(NODE).nth(i)) for i in range(len(Probe.DATASETS))
            ]
            for index, first in enumerate(boxes):
                for second in boxes[index + 1 :]:
                    overlap = (
                        first.x < second.right
                        and second.x < first.right
                        and first.y < second.bottom
                        and second.y < first.bottom
                    )
                    assert not overlap, (
                        f"nodes overlap in mode {mode}: {first} {second}"
                    )

        names_height = Css.box(node).height
        _switch_mode(page, "all fields")
        assert Css.box(node).height > keys_only > names_height

    def test_key_only_by_default_then_all_fields_and_names(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        _open_view(page, stand, seeded)
        node = page.locator(f'{NODE}[data-dataset="orders_raw"]')

        expect(node.locator(".ds-node__column")).to_have_count(Probe.KEY_COLUMNS)

        _switch_mode(page, "all fields")
        expect(node.locator(".ds-node__column")).to_have_count(Probe.ALL_COLUMNS)
        assert "mode=ALL_FIELDS" in page.url

        _switch_mode(page, "names")
        expect(node.locator(".ds-node__column")).to_have_count(0)
        assert "mode=TABLE_NAME" in page.url

    def test_click_opens_details_and_highlights_neighbours(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        _open_view(page, stand, seeded)

        page.locator(f'{NODE}[data-dataset="orders_stg"]').click()

        panel = page.get_by_test_id("detail-panel")
        expect(panel).to_have_attribute("data-dataset", "orders_stg")
        expect(panel.get_by_test_id("detail-columns").locator("tr")).to_have_count(
            Probe.ALL_COLUMNS
        )
        expect(
            panel.get_by_test_id("detail-incoming").locator(".detail__flow")
        ).to_have_count(1)
        expect(
            panel.get_by_test_id("detail-outgoing").locator(".detail__flow")
        ).to_have_count(1)
        assert f"active={seeded.probe.id_of('orders_stg')}" in page.url

        active = page.locator(f'{NODE}[data-dataset="orders_stg"]')
        expect(active).to_have_attribute("data-active", "true")
        # рамка меняется с переходом: ждём конечное значение, а не кадр анимации
        expect(active).to_have_css("border-color", tokens.rgb("signal"))

        expect(page.locator(f'{NODE}[data-dataset="orders_raw"]')).to_have_attribute(
            "data-highlighted", "true"
        )
        expect(page.locator(f'{NODE}[data-dataset="sales_dm"]')).to_have_attribute(
            "data-highlighted", "true"
        )
        expect(page.locator(f'{NODE}[data-dataset="customers_raw"]')).to_have_attribute(
            "data-highlighted", "false"
        )

        panel.get_by_role("button", name="close details").click()
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)
        assert "active=" not in page.url

    def test_left_pane_lists_groups_and_hides_datasets(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        _open_view(page, stand, seeded)
        pane = page.get_by_test_id("left-pane")

        expect(pane.locator(".pane__group")).to_have_count(len(Probe.LAYERS))
        expect(pane.get_by_test_id("pane-item")).to_have_count(len(Probe.DATASETS))

        pane.get_by_role("button", name="hide customers_raw").click()
        expect(page.locator(f'{NODE}[data-dataset="customers_raw"]')).to_have_count(0)
        expect(page.locator(EDGE_LABEL)).to_have_count(len(Probe.FLOWS) - 1)
        assert f"hidden={seeded.probe.id_of('customers_raw')}" in page.url

        pane.get_by_role("button", name="show customers_raw").click()
        expect(page.locator(f'{NODE}[data-dataset="customers_raw"]')).to_have_count(1)

        pane.get_by_label("find a dataset").fill("sales")
        expect(pane.get_by_test_id("pane-item")).to_have_count(1)

    def test_url_state_is_restored(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        active = seeded.probe.id_of("sales_dm")
        _open_view(page, stand, seeded, f"?active={active}&mode=TABLE_NAME")

        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-dataset", "sales_dm"
        )
        expect(
            page.locator(f'{NODE}[data-dataset="sales_dm"] .ds-node__column')
        ).to_have_count(0)

    def test_narrow_screen_keeps_the_scene_without_horizontal_scroll(
        self,
        browser: Browser,
        stand: StandProcess,
        seeded: Seeded,
        auth_cookies: list[Any],
    ) -> None:
        context = browser.new_context(viewport=NARROW)
        context.add_cookies(auth_cookies)
        narrow = context.new_page()
        try:
            _open_view(narrow, stand, seeded)
            assert no_horizontal_scroll(narrow)
            expect(narrow.get_by_test_id("left-pane")).to_have_count(0)

            narrow.locator(f'{NODE}[data-dataset="orders_raw"]').click()
            expect(narrow.get_by_test_id("detail-panel")).to_be_visible()
            assert no_horizontal_scroll(narrow)
        finally:
            context.close()


class TestDraftPage:
    def test_draft_shows_added_dataset_with_diff(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        _open_draft(page, stand, seeded)

        expect(page.get_by_test_id("page-title")).to_have_text("look edits")
        expect(page.locator(NODE)).to_have_count(len(Probe.DATASETS) + 1)

        added = page.locator(f'{NODE}[data-dataset="{Probe.DRAFT_DATASET}"]')
        expect(added).to_have_attribute("data-status", "added")
        expect(added).to_have_css("border-color", tokens.rgb("done"))
        expect(added.locator(".ds-node__status")).to_have_text("added")

        page.get_by_role("button", name="diff").click()
        expect(added).to_have_attribute("data-status", "unchanged")
        assert "diff=0" in page.url


class TestIndexPage:
    def test_index_lists_views_and_drafts(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        page.goto(f"{stand.config.base_url}/catalog/")
        index = page.get_by_test_id("index-page")

        expect(index.get_by_role("link", name="look view")).to_be_visible()
        expect(index.get_by_role("link", name="look edits")).to_be_visible()

        index.get_by_role("link", name="look view").click()
        page.wait_for_selector(READY, timeout=30_000)
        assert re.search(rf"/catalog/views/{seeded.view_id}$", page.url.split("?")[0])


def test_page_is_served_with_stamp(
    stand: StandProcess, auth_cookies: list[Any]
) -> None:
    """Сервер вписывает base href и конфиг страницы; без входа страница отдаётся."""
    response = httpx.get(
        f"{stand.config.base_url}/catalog/views/anything", timeout=30.0
    )

    assert response.status_code == 200
    assert f'<base href="{stand.config.url_prefix}/catalog/">' in response.text
    stamped = re.search(r"window.__BOBA_PAGE__ = (\{.*?\});", response.text)
    assert stamped is not None
    config = json.loads(stamped.group(1))
    assert config["apiPrefix"] == f"{stand.config.url_prefix}/api/catalog"
    assert config["prefix"] == stand.config.url_prefix
