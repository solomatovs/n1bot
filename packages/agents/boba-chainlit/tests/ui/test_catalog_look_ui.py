"""Внешний вид страницы процесса: дорожки слоёв слева направо, карточки узлов
с колонками из источника, рёбра потоков, список, тулбар, панель деталей,
режимы показа, diff черновика, узкий экран, вход без списков. Процесс сеется
через JSON API живого стенда над собственным источником.

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

import httpx
import pytest
from catalog_ui import (
    Api,
    FlowSpec,
    Objects,
    ProcessSeed,
    ProcessSpec,
    Selector,
    api_client,
)
from playwright.sync_api import Browser, Page, ViewportSize, expect

from boba.stand.ui.look import Css, Tokens, no_horizontal_scroll
from boba.stand.ui.stand import REPO_ROOT, StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}

TOKENS_CSS = (
    REPO_ROOT / "packages/agents/boba-chainlit/web/catalog/src/styles/tokens.css"
)

READY = Selector.READY.value
NODE = Selector.NODE.value
LANE = Selector.LANE.value
EDGE_LABEL = Selector.EDGE_LABEL.value


class Look:
    """Процесс стенда: три слоя, пять таблиц, два вида загрузки, три потока;
    шестая таблица returns_raw есть в источнике, но в процесс её кладёт
    только черновик."""

    SOURCE: ClassVar[str] = "look_prod"
    LAYERS: ClassVar[tuple[str, ...]] = ("look_raw", "look_stg", "look_dm")
    TABLES: ClassVar[dict[str, str]] = {
        "orders_raw": "look_raw",
        "customers_raw": "look_raw",
        "orders_stg": "look_stg",
        "customers_stg": "look_stg",
        "sales_dm": "look_dm",
    }
    FLOWS: ClassVar[tuple[FlowSpec, ...]] = (
        FlowSpec("orders_raw", "orders_stg", "hashkey", {"hash_columns": ["id"]}),
        FlowSpec("customers_raw", "customers_stg", "full"),
        FlowSpec("orders_stg", "sales_dm", "full"),
    )
    KEY_COLUMNS: ClassVar[int] = 1
    ALL_COLUMNS: ClassVar[int] = len(Objects.COLUMNS)
    DRAFT_TABLE: ClassVar[str] = "returns_raw"

    @classmethod
    def spec(cls) -> ProcessSpec:
        return ProcessSpec(
            source_name=cls.SOURCE,
            layers=cls.LAYERS,
            tables={**cls.TABLES, cls.DRAFT_TABLE: cls.LAYERS[0]},
            kinds=(
                {"name": "full", "fields": []},
                {
                    "name": "hashkey",
                    "fields": [
                        {
                            "name": "hash_columns",
                            "type": "columns",
                            "side": "source",
                            "required": True,
                            "description": "",
                        }
                    ],
                },
            ),
            flows=cls.FLOWS,
            id_base=0xA000,
        )


class LookSeed(ProcessSeed):
    """Сид look-модуля: returns_raw есть в источнике, но не в процессе."""

    def operations(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for op in super().operations():
            if op["op"] == "add_node" and op["node"]["id"] == self.id_of(
                Look.DRAFT_TABLE
            ):
                continue

            ops.append(op)

        return ops

    def draft_operations(self) -> list[dict[str, Any]]:
        return [self.node_op(Look.DRAFT_TABLE, Look.LAYERS[0])]


@dataclass(frozen=True)
class Seeded:
    """Что посеяно: вид на весь процесс и черновик с добавленным узлом."""

    seed: LookSeed
    view_id: str
    draft_id: str

    def node(self, name: str) -> str:
        return self.seed.node(name)


@pytest.fixture(scope="module")
def seeded(stand: StandProcess) -> Iterator[Seeded]:
    """Источник, процесс, вид и черновик через API: публикуется ровно один раз
    на модуль, на выходе черновик отменяется, вид удаляется, посеянное
    снимается публикацией и источник удаляется."""
    with api_client(stand, "admin") as admin:
        api = Api(admin)
        seed = LookSeed(api, Look.spec())
        seed.publish("look seed")
        view_id = api.create_view("look view", [], [])
        edits = api.new_draft("look edits")
        api.append(edits, seed.draft_operations())

    seeded = Seeded(seed=seed, view_id=view_id, draft_id=edits)
    try:
        yield seeded
    finally:
        with api_client(stand, "admin") as admin:
            api = Api(admin)
            seed.api = api
            api.discard(seeded.draft_id)
            api.delete_view(seeded.view_id)
            seed.cleanup()


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
        expect(page.locator(NODE)).to_have_count(len(Look.TABLES))
        expect(page.locator(LANE)).to_have_count(len(Look.LAYERS))
        expect(page.locator(EDGE_LABEL)).to_have_count(len(Look.FLOWS))

        labels = sorted(page.locator(EDGE_LABEL).all_inner_texts())
        assert labels == ["full", "full", "hashkey"]

    def test_lanes_follow_layer_order_left_to_right(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Партиции ELK: слой-источник левее приёмника, дорожки не пересекаются."""
        _open_view(page, stand, seeded)

        lefts: list[float] = []
        for layer in Look.LAYERS:
            lane = page.locator(f'{LANE}[data-layer="{layer}"]')
            expect(lane).to_have_count(1)
            lefts.append(Css.box(lane).x)

        assert lefts == sorted(lefts), f"lanes are not ordered by layer: {lefts}"

        boxes = [
            Css.box(page.locator(f'{LANE}[data-layer="{layer}"]'))
            for layer in Look.LAYERS
        ]
        for previous, current in pairwise(boxes):
            assert previous.right <= current.x + 1, "lanes overlap"

        for table, layer in Look.TABLES.items():
            node = page.locator(seeded.node(table))
            lane = page.locator(f'{LANE}[data-layer="{layer}"]')
            assert Css.box(lane).contains(Css.box(node), slack=2), (
                f"{table} is outside its lane {layer}"
            )

    def test_nodes_are_laid_out_by_measured_size_without_overlap(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Раскладка идёт по замеру DOM: карточки не пересекаются в любом режиме,
        а карточка со всеми колонками выше карточки с одними ключами."""
        _open_view(page, stand, seeded)
        node = page.locator(seeded.node("orders_raw"))
        keys_only = Css.box(node).height

        for mode in ("all fields", "names"):
            _switch_mode(page, mode)
            boxes = [
                Css.box(page.locator(NODE).nth(i)) for i in range(len(Look.TABLES))
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
        node = page.locator(seeded.node("orders_raw"))

        expect(node.locator(".ds-node__column")).to_have_count(Look.KEY_COLUMNS)

        _switch_mode(page, "all fields")
        expect(node.locator(".ds-node__column")).to_have_count(Look.ALL_COLUMNS)
        assert "mode=ALL_FIELDS" in page.url

        _switch_mode(page, "names")
        expect(node.locator(".ds-node__column")).to_have_count(0)
        assert "mode=TABLE_NAME" in page.url

    def test_click_opens_details_and_highlights_neighbours(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        _open_view(page, stand, seeded)

        page.locator(seeded.node("orders_stg")).click()

        panel = page.get_by_test_id("detail-panel")
        expect(panel).to_have_attribute("data-node", seeded.seed.address("orders_stg"))
        expect(panel.get_by_test_id("detail-columns").locator("tr")).to_have_count(
            Look.ALL_COLUMNS
        )
        expect(panel.get_by_test_id("node-card")).to_be_visible(timeout=15_000)
        expect(
            panel.get_by_test_id("detail-incoming").locator(".detail__flow")
        ).to_have_count(1)
        expect(
            panel.get_by_test_id("detail-outgoing").locator(".detail__flow")
        ).to_have_count(1)
        assert f"active={seeded.seed.id_of('orders_stg')}" in page.url

        active = page.locator(seeded.node("orders_stg"))
        expect(active).to_have_attribute("data-active", "true")
        # рамка меняется с переходом: ждём конечное значение, а не кадр анимации
        expect(active).to_have_css("border-color", tokens.rgb("signal"))

        expect(page.locator(seeded.node("orders_raw"))).to_have_attribute(
            "data-highlighted", "true"
        )
        expect(page.locator(seeded.node("sales_dm"))).to_have_attribute(
            "data-highlighted", "true"
        )
        expect(page.locator(seeded.node("customers_raw"))).to_have_attribute(
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

        expect(pane.locator(".pane__group")).to_have_count(len(Look.LAYERS))
        expect(pane.get_by_test_id("pane-item")).to_have_count(len(Look.TABLES))

        pane.get_by_role("button", name="hide customers_raw").click()
        expect(page.locator(seeded.node("customers_raw"))).to_have_count(0)
        expect(page.locator(EDGE_LABEL)).to_have_count(len(Look.FLOWS) - 1)
        assert f"hidden={seeded.seed.id_of('customers_raw')}" in page.url

        pane.get_by_role("button", name="show customers_raw").click()
        expect(page.locator(seeded.node("customers_raw"))).to_have_count(1)

        pane.get_by_label("find a node").fill("sales")
        expect(pane.get_by_test_id("pane-item")).to_have_count(1)

        pane.get_by_role("tab", name="sources").click()
        expect(pane).to_have_attribute("data-tab", "sources")
        branch = pane.locator(
            f'[data-testid="source-branch"][data-source="{Look.SOURCE}"]'
        )
        expect(branch).to_be_visible()
        assert "pane=sources" in page.url

    def test_url_state_is_restored(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        active = seeded.seed.id_of("sales_dm")
        _open_view(page, stand, seeded, f"?active={active}&mode=TABLE_NAME")

        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", seeded.seed.address("sales_dm")
        )
        expect(
            page.locator(f"{seeded.node('sales_dm')} .ds-node__column")
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

            narrow.locator(seeded.node("orders_raw")).click()
            expect(narrow.get_by_test_id("detail-panel")).to_be_visible()
            assert no_horizontal_scroll(narrow)
        finally:
            context.close()


class TestDraftPage:
    def test_draft_shows_added_node_with_diff(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        _open_draft(page, stand, seeded)

        expect(page.get_by_test_id("page-title")).to_have_text("look edits")
        expect(page.locator(NODE)).to_have_count(len(Look.TABLES) + 1)

        added = page.locator(seeded.node(Look.DRAFT_TABLE))
        expect(added).to_have_attribute("data-status", "added")
        expect(added).to_have_css("border-color", tokens.rgb("done"))
        expect(added.locator(".ds-node__status")).to_have_text("added")

        page.get_by_role("button", name="diff").click()
        expect(added).to_have_attribute("data-status", "unchanged")
        assert "diff=0" in page.url


class TestPublishedPage:
    def test_entry_shows_the_process_with_menus(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Вход — сам процесс: узлы на холсте, диаграммы и черновики в диалогах
        шапки, ссылка из диаграмм открывает вид."""
        page.goto(f"{stand.config.base_url}/catalog/")
        page.wait_for_selector(READY, timeout=30_000)
        catalog = page.get_by_test_id("catalog-page")
        expect(catalog).to_have_attribute("data-source", "published")
        expect(page.locator(seeded.node("orders_raw"))).to_be_visible()

        page.get_by_test_id("edit-button").click()
        drafts = page.locator('[data-dialog="drafts"]')
        expect(drafts.get_by_role("link", name="look edits")).to_be_visible()
        drafts.get_by_role("button", name="close dialog").click()
        expect(drafts).to_have_count(0)

        page.get_by_test_id("diagrams-button").click()
        diagrams = page.locator('[data-dialog="diagrams"]')
        expect(diagrams.get_by_role("link", name="look view")).to_be_visible()
        diagrams.get_by_role("link", name="look view").click()
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
