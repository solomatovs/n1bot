"""Внешний вид страницы процесса: дорожки слоёв слева направо, карточки узлов
с колонками из снимка, рёбра потоков с числом колонок, список, тулбар, панель
деталей, режимы показа, diff черновика, полоса черновика, узкий экран, вход
списком процессов. Процесс сеется через JSON API живого стенда над
собственным подключением.

Ожидания цветов — из tokens.css сборки страницы (Tokens); геометрия — из
bounding box узлов и дорожек.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, ClassVar

import httpx
import pytest
from catalog_ui import (
    Api,
    Canvas,
    CatalogPage,
    FlowSpec,
    Objects,
    ProcessSeed,
    ProcessSpec,
    Selector,
    Viewport,
    api_client,
)
from playwright.sync_api import Browser, Page, ViewportSize, expect

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.look import Css, Tokens, close, no_horizontal_scroll
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

READY = Selector.READY.value
NODE = Selector.NODE.value
FRAME = Selector.FRAME.value
EDGE_LABEL = Selector.EDGE_LABEL.value


class Look:
    """Процесс стенда: три группы, пять таблиц с позициями сеткой, три потока;
    шестая таблица returns_raw есть в снимке, но в процесс её кладёт только
    черновик — без позиции, её раскладывает холст."""

    PROCESS: ClassVar[str] = "look_process"
    CONNECTION: ClassVar[str] = "look_prod"
    GROUPS: ClassVar[tuple[str, ...]] = ("look_raw", "look_stg", "look_dm")
    TABLES: ClassVar[dict[str, str]] = {
        "orders_raw": "look_raw",
        "customers_raw": "look_raw",
        "orders_stg": "look_stg",
        "customers_stg": "look_stg",
        "sales_dm": "look_dm",
    }
    FLOWS: ClassVar[tuple[FlowSpec, ...]] = (
        FlowSpec(
            "orders_raw", "orders_stg", (("id", "id"), ("name", "name")), "orders"
        ),
        FlowSpec("customers_raw", "customers_stg", (("id", "id"),), "customers"),
        FlowSpec("orders_stg", "sales_dm"),
    )
    KEY_COLUMNS: ClassVar[int] = 1
    ALL_COLUMNS: ClassVar[int] = len(Objects.COLUMNS)
    DRAFT_TABLE: ClassVar[str] = "returns_raw"

    @classmethod
    def spec(cls) -> ProcessSpec:
        return ProcessSpec(
            process_name=cls.PROCESS,
            connection_name=cls.CONNECTION,
            groups=cls.GROUPS,
            tables={**cls.TABLES, cls.DRAFT_TABLE: cls.GROUPS[0]},
            flows=cls.FLOWS,
            id_base=0xA000,
        )


class LookSeed(ProcessSeed):
    """Сид look-модуля: returns_raw есть в снимке, но не в процессе."""

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
        """Узел черновика без позиции: его место считает холст."""
        op = self.node_op(Look.DRAFT_TABLE, Look.GROUPS[0])
        op["node"]["position"] = None
        return [op]


@dataclass(frozen=True)
class Seeded:
    """Что посеяно: опубликованный процесс и черновик с добавленным узлом."""

    seed: LookSeed
    draft_id: str

    def node(self, name: str) -> str:
        return self.seed.node(name)

    @property
    def process_id(self) -> str:
        return self.seed.process_id


@pytest.fixture(scope="module")
def seeded(stand: StandProcess, stand_db: StandDatabase) -> Iterator[Seeded]:
    """Подключение, процесс и черновик через API: публикуется ровно один раз
    на модуль, на выходе черновик отменяется, процесс и подключение удаляются."""
    with api_client(stand, "admin") as admin:
        api = Api(admin, stand_db)
        seed = LookSeed(api, Look.spec())
        seed.publish("look seed")
        edits = api.new_draft(seed.process_id, "look edits")
        api.append(edits, seed.draft_operations())

    seeded = Seeded(seed=seed, draft_id=edits)
    try:
        yield seeded
    finally:
        with api_client(stand, "admin") as admin:
            api = Api(admin, stand_db)
            seed.api = api
            api.discard(seeded.draft_id)
            seed.cleanup()


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load()


@pytest.fixture
def page(
    browser: Browser, stand: StandProcess, auth_cookies: list[Any]
) -> Iterator[Page]:
    context = browser.new_context(viewport=Viewport.WIDE)
    context.add_cookies(auth_cookies)
    opened = context.new_page()
    try:
        yield opened
    finally:
        context.close()


def _switch_mode(page: Page, mode: str) -> None:
    """Режим карточек через тулбар с ожиданием новой раскладки."""

    def click() -> None:
        page.get_by_role("tab", name=mode).click()

    Canvas.relayout(page, click)


def _open_view(
    page: Page, stand: StandProcess, seeded: Seeded, query: str = ""
) -> None:
    CatalogPage.PROCESS.open(page, stand, query, process_id=seeded.process_id)
    page.wait_for_selector(READY, timeout=30_000)
    page.wait_for_selector(NODE, timeout=30_000)


def _open_draft(
    page: Page, stand: StandProcess, seeded: Seeded, query: str = ""
) -> None:
    CatalogPage.DRAFT.open(page, stand, query, draft_id=seeded.draft_id)
    page.wait_for_selector(READY, timeout=30_000)
    page.wait_for_selector(NODE, timeout=30_000)


class TestProcessPage:
    def test_nodes_edges_and_frames_are_rendered(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        _open_view(page, stand, seeded)

        expect(page.get_by_test_id("page-title")).to_have_text(Look.PROCESS)
        expect(page.locator(NODE)).to_have_count(len(Look.TABLES))
        expect(page.locator(FRAME)).to_have_count(len(Look.GROUPS))
        # ярлык ребра — описание потока; поток без описания идёт без ярлыка
        described = [flow for flow in Look.FLOWS if flow.description != ""]
        expect(page.locator(EDGE_LABEL)).to_have_count(len(described))
        labels = sorted(page.locator(EDGE_LABEL).all_inner_texts())
        assert labels == sorted(flow.description for flow in described)

    def test_cards_stand_where_the_process_puts_them(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Позиции из процесса: карточки стоят сеткой сеятеля — колонки групп
        слева направо, рамки групп обнимают свои карточки и не пересекаются."""
        _open_view(page, stand, seeded)

        lefts: list[float] = []
        for group in Look.GROUPS:
            frame = page.locator(f'{FRAME}[data-group="{group}"]')
            expect(frame).to_have_count(1)
            lefts.append(Css.box(frame).x)

        assert lefts == sorted(lefts), f"frames are not in seed order: {lefts}"

        boxes = [
            Css.box(page.locator(f'{FRAME}[data-group="{group}"]'))
            for group in Look.GROUPS
        ]
        for previous, current in pairwise(boxes):
            assert previous.right <= current.x + 1, "frames overlap"

        for table, group in Look.TABLES.items():
            node = page.locator(seeded.node(table))
            frame = page.locator(f'{FRAME}[data-group="{group}"]')
            assert Css.box(frame).contains(Css.box(node), slack=2), (
                f"{table} is outside its frame {group}"
            )

        # ряд группы: orders_raw над customers_raw, с зазором сетки
        orders = Css.box(page.locator(seeded.node("orders_raw")))
        customers = Css.box(page.locator(seeded.node("customers_raw")))
        assert abs(orders.x - customers.x) < 2
        assert orders.bottom < customers.y

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

        # режим ключей: ключ id и колонка name, по которой идёт линия потока
        expect(node.locator(".proc-node__column")).to_have_count(Look.KEY_COLUMNS + 1)

        _switch_mode(page, "all fields")
        expect(node.locator(".proc-node__column")).to_have_count(Look.ALL_COLUMNS)
        assert "mode=ALL_FIELDS" in page.url

        _switch_mode(page, "names")
        expect(node.locator(".proc-node__column")).to_have_count(0)
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
            panel.get_by_test_id("detail-incoming").get_by_test_id("detail-flow")
        ).to_have_count(1)
        expect(
            panel.get_by_test_id("detail-outgoing").get_by_test_id("detail-flow")
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

    def test_lines_run_between_columns_and_light_up_with_the_card(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        """Поток — линия на каждую пару колонок от ручки к ручке; у выбранной
        карточки линии и колонки-участники подсвечены цветом сигнала, типы
        колонок видны, ручки колонок появляются при наведении."""
        _open_view(page, stand, seeded, "?mode=ALL_FIELDS")
        pairs = sum(len(flow.columns) for flow in Look.FLOWS)
        without_pairs = sum(1 for flow in Look.FLOWS if not flow.columns)
        expect(page.locator(".react-flow__edge")).to_have_count(pairs + without_pairs)

        orders = page.locator(seeded.node("orders_raw"))
        stg = page.locator(seeded.node("orders_stg"))
        # линия id → id идёт от строки id одной карточки к строке id другой
        source_row = Css.box(orders.locator('[data-column="id"]'))
        target_row = Css.box(stg.locator('[data-column="id"]'))
        line = Css.box(
            page.locator(f'.react-flow__edge[data-id="{seeded.seed.flow_id(0)}#0"]')
        )
        assert abs(line.x - source_row.right) < 8, (line, source_row)
        assert abs(line.right - target_row.x) < 12, (line, target_row)

        column_type = orders.locator('[data-column="id"] .proc-node__column-type')
        expect(column_type).to_have_css("opacity", "0")

        orders.locator(".proc-node__header").click()
        expect(column_type).to_have_css("opacity", "1")
        lit = orders.locator('[data-column="id"]')
        expect(lit).to_have_attribute("data-lit", "true")
        expect(page.locator(".flow-edge--lit")).to_have_count(2)
        expect(page.locator(".flow-edge--lit").first).to_have_css(
            "stroke", tokens.rgb("signal")
        )
        expect(page.locator(".flow-edge__particle")).to_have_count(6)
        handle = lit.locator(".react-flow__handle.source")
        expect(handle).to_have_css("background-color", tokens.rgb("signal"))

        unlit = orders.locator('[data-column="updated_at"]')
        expect(unlit).to_have_attribute("data-lit", "false")
        page.mouse.move(5, 5)
        expect(unlit.locator(".react-flow__handle.source")).to_have_css("opacity", "0")
        unlit.hover()
        expect(unlit.locator(".react-flow__handle.source")).to_have_css("opacity", "1")

    def test_left_pane_lists_groups_and_hides_datasets(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        _open_view(page, stand, seeded)
        pane = page.get_by_test_id("left-pane")

        expect(pane.get_by_test_id("nodes-group")).to_contain_text(
            f"nodes · {len(Look.TABLES)}"
        )
        expect(pane.get_by_test_id("pane-item")).to_have_count(len(Look.TABLES))

        pane.get_by_role("button", name="hide customers_raw").click()
        expect(page.locator(seeded.node("customers_raw"))).to_have_count(0)
        expect(page.locator(EDGE_LABEL)).to_have_count(1)
        assert f"hidden={seeded.seed.id_of('customers_raw')}" in page.url

        pane.get_by_role("button", name="show customers_raw").click()
        expect(page.locator(seeded.node("customers_raw"))).to_have_count(1)

        pane.get_by_label("find a node").fill("sales")
        expect(pane.get_by_test_id("pane-item")).to_have_count(1)

        pane.get_by_role("tab", name="connections").click()
        expect(pane).to_have_attribute("data-tab", "connections")
        branch = pane.locator(
            f'[data-testid="connection-branch"][data-connection="{Look.CONNECTION}"]'
        )
        expect(branch).to_be_visible()
        assert "pane=connections" in page.url

        # вкладки равноправны: у процесса тоже явный адрес
        pane.get_by_role("tab", name="process").click()
        expect(pane).to_have_attribute("data-tab", "process")
        assert "pane=process" in page.url

    def test_url_state_is_restored(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        active = seeded.seed.id_of("sales_dm")
        _open_view(page, stand, seeded, f"?active={active}&mode=TABLE_NAME")

        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", seeded.seed.address("sales_dm")
        )
        expect(
            page.locator(f"{seeded.node('sales_dm')} .proc-node__column")
        ).to_have_count(0)

    def test_detail_panel_is_resized_by_its_grip_and_the_width_is_kept(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        """За левый край панели деталей тянут: колонка меняет ширину в
        пределах токенов, ширина переживает перезагрузку страницы и другой
        процесс, полоса захвата стоит на всю высоту панели."""
        _open_view(page, stand, seeded)
        page.locator(seeded.node("orders_stg")).click()
        detail = page.locator(".page__detail")
        expect(page.get_by_test_id("detail-panel")).to_be_visible()
        before = Css.box(detail)
        grip = page.get_by_test_id("detail-grip")
        grip_box = Css.box(grip)
        assert abs(grip_box.height - before.height) <= 1
        assert abs(grip_box.x - before.x) <= tokens.px("s1") + 1
        expect(grip).to_have_css("cursor", "col-resize")

        page.mouse.move(grip_box.x + grip_box.width / 2, grip_box.y + 200)
        page.mouse.down()
        page.mouse.move(grip_box.x - 150, grip_box.y + 200, steps=10)
        page.mouse.up()

        # левый край панели встаёт под указатель
        after = Css.box(detail)
        assert abs(after.x - (grip_box.x - 150)) <= 2, (grip_box, after)
        assert after.width > before.width + 100
        scene = Css.box(page.locator(".page__scene"))
        assert scene.width < Viewport.WIDE["width"] - after.width
        body = page.locator(".page__body")
        expect(body).to_have_attribute("data-detail-width", str(round(after.width)))
        no_horizontal_scroll(page)

        page.reload()
        page.wait_for_selector(READY, timeout=30_000)
        expect(page.get_by_test_id("detail-panel")).to_be_visible()
        kept = Css.box(detail)
        assert abs(kept.width - after.width) <= 2, (after, kept)

        # уже панели минимума не бывает
        grip_box = Css.box(grip)
        page.mouse.move(grip_box.x + grip_box.width / 2, grip_box.y + 200)
        page.mouse.down()
        page.mouse.move(grip_box.x + 900, grip_box.y + 200, steps=10)
        page.mouse.up()
        assert Css.box(detail).width == tokens.px("w-detail-min")

    def test_narrow_screen_keeps_the_scene_without_horizontal_scroll(
        self,
        browser: Browser,
        stand: StandProcess,
        seeded: Seeded,
        auth_cookies: list[Any],
    ) -> None:
        context = browser.new_context(viewport=Viewport.NARROW)
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


MEDIUM: ViewportSize = {"width": 1100, "height": 800}


class TestGrid:
    """Сетка виджетов: высоты контролов из токенов, ряд шапки выровнен по
    центру, панели у минимумов на среднем экране, ящики на узком."""

    def test_controls_share_the_height_scale(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        _open_draft(page, stand, seeded)
        ctl = tokens.px("h-ctl")
        small = tokens.px("h-ctl-sm")

        topbar = page.locator(".topbar")
        actions = page.get_by_test_id("process-actions")
        heights = {
            "icon-button": Css.box(topbar.locator(".icon-btn").first).height,
            "button-sm": Css.box(actions.get_by_test_id("publish-button")).height,
            "chip": Css.box(topbar.locator(".chip").first).height,
        }
        assert heights["icon-button"] == ctl, heights
        assert heights["button-sm"] == small, heights
        assert heights["chip"] == tokens.px("h-chip"), heights
        # полоса действий закреплена над списком и не уезжает с ним
        assert Css.box(actions).y < Css.box(page.get_by_test_id("processes-group")).y
        bar = page.locator('[data-notice="draft-bar"]')
        expect(bar).to_have_css("border-left-color", tokens.rgb("signal"))

        toolbar = page.get_by_test_id("canvas-toolbar")
        zoom = Css.box(toolbar.locator(".icon-btn").first)
        modes = Css.box(toolbar.get_by_role("tablist"))
        assert zoom.height == ctl
        assert modes.height == ctl
        assert close(zoom.y + zoom.height / 2, modes.y + modes.height / 2)

        pane = page.get_by_test_id("left-pane")
        assert Css.box(pane.get_by_label("find a node")).height == ctl
        assert Css.box(pane.get_by_test_id("pane-item").first).height == ctl

        page.locator(seeded.node("orders_raw")).click()
        panel = page.get_by_test_id("detail-panel")
        expect(panel).to_be_visible()
        row = panel.get_by_test_id("detail-columns").locator("tbody tr").first
        assert Css.box(row).height == tokens.px("h-row")
        for button in panel.locator("header .icon-btn").all():
            assert Css.box(button).height == small

    def test_medium_screen_shrinks_the_panes_to_their_minimums(
        self,
        browser: Browser,
        stand: StandProcess,
        seeded: Seeded,
        auth_cookies: list[Any],
        tokens: Tokens,
    ) -> None:
        context = browser.new_context(viewport=MEDIUM)
        context.add_cookies(auth_cookies)
        medium = context.new_page()
        try:
            _open_view(medium, stand, seeded)
            pane = Css.box(medium.locator(".page__pane"))
            assert pane.width == tokens.px("w-pane-min")
            medium.locator(seeded.node("orders_raw")).click()
            expect(medium.get_by_test_id("detail-panel")).to_be_visible()
            detail = Css.box(medium.locator(".page__detail"))
            assert detail.width == tokens.px("w-detail-min")
            assert no_horizontal_scroll(medium)
            expect(medium.locator(".topbar__hint")).to_be_hidden()
        finally:
            context.close()

    def test_narrow_screen_collapses_button_labels_and_dialogs_fit(
        self,
        browser: Browser,
        stand: StandProcess,
        seeded: Seeded,
        auth_cookies: list[Any],
    ) -> None:
        context = browser.new_context(viewport=Viewport.NARROW)
        context.add_cookies(auth_cookies)
        narrow = context.new_page()
        try:
            _open_view(narrow, stand, seeded)
            narrow.get_by_role("button", name="show the left pane").click()
            narrow.get_by_test_id("share-button").click()
            dialog = narrow.get_by_role("dialog")
            expect(dialog).to_be_visible()
            box = Css.box(dialog)
            assert box.x >= 0
            assert box.right <= Viewport.NARROW["width"]
            assert no_horizontal_scroll(narrow)
        finally:
            context.close()


SHORT: ViewportSize = {"width": 1100, "height": 480}


class TestViewportFit:
    """Ничего не уходит за окно: документ не скроллится, каждая область
    скроллится сама. Диалог не выше окна, его подвал с кнопками закреплён и
    виден без прокрутки, тело диалога прокручивается; страница-список
    прокручивается внутри себя."""

    def test_tall_dialog_fits_a_short_window_and_keeps_its_footer(
        self,
        browser: Browser,
        stand: StandProcess,
        auth_cookies: list[Any],
        tokens: Tokens,
    ) -> None:
        context = browser.new_context(viewport=SHORT)
        context.add_cookies(auth_cookies)
        page = context.new_page()
        try:
            CatalogPage.CONNECTIONS.open(page, stand)
            page.get_by_test_id("add-connection").click()
            form = page.get_by_test_id("connection-form")
            form.get_by_label("profile.kind").select_option("postgres")
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()

            box = Css.box(dialog)
            assert box.y >= 0
            assert box.bottom <= SHORT["height"], box
            body = dialog.locator(".dialog__body")
            assert page.evaluate(
                "el => el.scrollHeight > el.clientHeight", body.element_handle()
            ), "the long form must scroll inside the dialog body"

            footer = form.get_by_test_id("save-connection")
            save = Css.box(footer)
            assert save.bottom <= SHORT["height"], save
            assert save.y >= box.y
            expect(footer).to_be_in_viewport()
            expect(form.get_by_label("connection name")).to_be_in_viewport()

            # подвал закреплён: после прокрутки тела кнопка на том же месте
            body.evaluate("el => { el.scrollTop = el.scrollHeight; }")
            assert abs(Css.box(footer).y - save.y) < 1
            expect(footer.locator("..")).to_have_css(
                "background-color", tokens.rgb("surface")
            )
            assert no_horizontal_scroll(page)
        finally:
            context.close()

    def test_index_page_scrolls_inside_itself(
        self, browser: Browser, stand: StandProcess, auth_cookies: list[Any]
    ) -> None:
        context = browser.new_context(viewport={"width": 1100, "height": 200})
        context.add_cookies(auth_cookies)
        page = context.new_page()
        try:
            CatalogPage.CONNECTIONS.open(page, stand)
            expect(page.get_by_test_id("connections-page")).to_be_visible()
            index = page.get_by_test_id("connections-page")
            assert page.evaluate(
                "el => el.scrollHeight > el.clientHeight && "
                "getComputedStyle(el).overflowY === 'auto'",
                index.element_handle(),
            ), "the list page must own its vertical scroll"
            assert page.evaluate(
                "() => document.documentElement.scrollHeight <= window.innerHeight"
            ), "the document itself must not scroll"
            assert no_horizontal_scroll(page)
        finally:
            context.close()


class TestDraftPage:
    def test_draft_shows_added_node_with_diff(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        _open_draft(page, stand, seeded)

        expect(page.get_by_test_id("page-title")).to_have_text(Look.PROCESS)
        expect(page.get_by_test_id("draft-name")).to_have_text("draft “look edits”")
        expect(page.locator(NODE)).to_have_count(len(Look.TABLES) + 1)

        added = page.locator(seeded.node(Look.DRAFT_TABLE))
        expect(added).to_have_attribute("data-status", "added")
        expect(added).to_have_css("border-color", tokens.rgb("done"))
        expect(added.locator(".proc-node__status")).to_have_text("added")

        page.get_by_role("button", name="diff").click()
        expect(added).to_have_attribute("data-status", "unchanged")
        assert "diff=0" in page.url


class TestEntryPage:
    def test_entry_lists_the_processes_and_opens_one(
        self, page: Page, stand: StandProcess, seeded: Seeded
    ) -> None:
        """Вход — список процессов с версией, числом узлов и черновиков; клик
        открывает процесс, черновик стоит во вкладке process."""
        CatalogPage.HOME.open(page, stand)
        listed = page.get_by_test_id("processes-list")
        row = listed.locator(f'li[data-process="{Look.PROCESS}"]')
        expect(row).to_contain_text("v1")
        expect(row).to_contain_text(f"{len(Look.TABLES)} nodes")
        # свой черновик — строкой под процессом с чипом draft
        expect(listed.locator('li[data-draft="look edits"]')).to_contain_text("draft")

        row.get_by_role("link", name=Look.PROCESS).click()
        page.wait_for_selector(READY, timeout=30_000)
        catalog = page.get_by_test_id("catalog-page")
        expect(catalog).to_have_attribute("data-source", "published")
        expect(page.locator(seeded.node("orders_raw"))).to_be_visible()
        expect(
            page.get_by_test_id("processes-list").get_by_role("link", name="look edits")
        ).to_be_visible()
        assert re.search(
            rf"/catalog/processes/{seeded.process_id}$", page.url.split("?")[0]
        )

    def test_entry_is_the_process_page_without_a_process(
        self, page: Page, stand: StandProcess, seeded: Seeded, tokens: Tokens
    ) -> None:
        """Вход — та же страница, что у процесса: топбар, левая панель с
        полосой «process» и списком процессов, сцена с шагами; вкладка
        connections показывает подключения, открытого процесса в панели нет."""
        CatalogPage.HOME.open(page, stand)
        catalog = page.get_by_test_id("catalog-page")
        expect(catalog).to_have_attribute("data-source", "home")
        expect(page.get_by_test_id("page-title")).to_have_text("processes")
        expect(page.locator(".topbar__hint")).to_contain_text("process")

        pane = page.get_by_test_id("left-pane")
        expect(pane).to_have_attribute("data-tab", "process")
        # полосы действий на входе нет: новый процесс — плюс у списка
        expect(page.get_by_test_id("process-actions")).to_have_count(0)
        processes = page.get_by_test_id("processes-group")
        expect(processes).to_contain_text("processes ·")
        plus = processes.get_by_test_id("new-process")
        expect(plus).to_have_accessible_name("new process")
        assert Css.box(plus).height == tokens.px("h-ctl-sm")
        expect(page.get_by_test_id("drafts-group")).to_have_count(0)
        expect(page.get_by_test_id("nodes-group")).to_have_count(0)

        scene = page.locator(".page__scene")
        # на входе одна строка подсказки, без шагов и кнопок
        expect(scene.get_by_test_id("home-hint")).to_be_visible()
        expect(scene.get_by_role("button")).to_have_count(0)
        expect(scene.locator(".empty__title")).to_have_css("color", tokens.rgb("ink"))

        page.get_by_role("tab", name="connections").click()
        expect(pane).to_have_attribute("data-tab", "connections")
        expect(page.get_by_test_id("connections-actions")).to_be_visible()
        connections = page.get_by_test_id("connections-group")
        expect(connections).to_contain_text("connections ·")
        expect(connections.get_by_test_id("add-connection")).to_have_accessible_name(
            "new connection"
        )
        no_horizontal_scroll(page)


def test_page_is_served_with_stamp(
    stand: StandProcess, auth_cookies: list[Any]
) -> None:
    """Сервер вписывает base href и конфиг страницы; без входа страница отдаётся."""
    response = httpx.get(
        CatalogPage.PROCESS.url(stand, process_id="anything"), timeout=30.0
    )

    assert response.status_code == 200
    assert f'<base href="{stand.config.url_prefix}/workflow/">' in response.text
    stamped = re.search(r"window.__BOBA_PAGE__ = (\{.*?\});", response.text)
    assert stamped is not None
    config = json.loads(stamped.group(1))
    assert config["apiPrefix"] == f"{stand.config.url_prefix}/api"
    assert config["prefix"] == stand.config.url_prefix
