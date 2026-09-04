"""Правки черновика на странице процесса: слой через подсказку имени, узел из
дерева источника кнопкой и перетаскиванием, форма узла, перенацеливание,
поток из панели и соединением на холсте, удаление, чужие порции и новые
версии по событиям, устаревание после новой версии источника и поднятие
привязок, публикация и перебазирование устаревшего черновика.

Модуль сеет свой процесс над источником ed_prod и на выходе публикует его
удаление, чтобы опубликованный каталог стенда остался таким, каким его ждут
соседние модули.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from catalog_ui import Api, Ed, Seed, Selector
from playwright.sync_api import Browser, FloatRect, Locator, Page, ViewportSize, expect

from boba.stand.ui.look import Css
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
EDITABLE = f'{Selector.PAGE}[data-editable="true"]'
LIVE_TIMEOUT_MS = 15_000


@pytest.fixture
def draft_id(
    catalog_api: Api, catalog_seed: Seed, request: pytest.FixtureRequest
) -> Iterator[str]:
    created = catalog_api.new_draft(f"edit {request.node.name}")
    try:
        yield created
    finally:
        catalog_api.discard(created)


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
    page.wait_for_selector(Selector.READY, timeout=30_000)
    page.wait_for_selector(EDITABLE, timeout=30_000)
    page.wait_for_selector(Selector.NODE, timeout=30_000)


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


def _drag(
    page: Page, source: Locator, target: Locator, offset: tuple[float, float]
) -> None:
    """HTML5-перетаскивание мышью: dragover срабатывает от второго движения,
    цель — точка со смещением от левого верхнего угла target."""
    start = source.bounding_box()
    end = target.bounding_box()
    assert start is not None
    assert end is not None

    page.mouse.move(*_centre(start))
    page.mouse.down()
    page.mouse.move(end["x"] + offset[0], end["y"] + offset[1], steps=8)
    page.mouse.move(end["x"] + offset[0] + 1, end["y"] + offset[1] + 1, steps=2)
    page.mouse.up()


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


def _node_addresses(state: dict[str, Any]) -> set[str]:
    addresses: set[str] = set()
    for node in state["snapshot"]["nodes"].values():
        addresses.add("/".join(node["ref"]["path"]))

    return addresses


def _open_source_tree(page: Page, seed: Seed) -> Locator:
    """Вкладка источников: раскрыть ed_prod, базу, схему public и группу
    таблиц; вернуть панель."""
    pane = page.get_by_test_id("left-pane")
    pane.get_by_role("tab", name="sources").click()
    branch = pane.locator(
        f'[data-testid="source-branch"][data-source="{seed.source_name}"]'
    )
    branch.get_by_role("button", name=f"expand source {seed.source_name}").click()

    for path in ("prod", "prod/public", "prod/public/tables"):
        item = branch.locator(f'[data-testid="tree-node"][data-path="{path}"]')
        expect(item).to_be_visible(timeout=15_000)
        item.locator(".tree__row").first.get_by_role("button", name="expand").click()

    return pane


def _pick_object(page: Page, seed: Seed, name: str) -> Locator:
    """Объект в дереве выбран: справа его панель."""
    pane = _open_source_tree(page, seed)
    pane.locator(seed.tree_object(name)).locator(".tree__label").click()
    panel = page.get_by_test_id("object-panel")
    expect(panel).to_have_attribute("data-object", seed.address(name))
    return panel


class TestPrompts:
    def test_add_layer_then_node_into_it(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Слой через подсказку имени; объект из дерева источника ставится в
        новый слой кнопкой панели объекта и открывается панелью узла."""
        _open_draft(page, stand, draft_id)

        page.get_by_role("button", name="layer", exact=True).click()
        _prompt_name(page, "layer-name", "ed_new")
        expect(page.locator('.pane__group[data-layer="ed_new"]')).to_be_visible()

        panel = _pick_object(page, catalog_seed, Ed.EVENTS)
        expect(panel).to_have_attribute("data-in-process", "false")
        panel.get_by_label("layer for the new node").select_option(label="ed_new")
        panel.get_by_role("button", name="add to layer").click()

        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible()
        expect(added).to_have_attribute("data-status", "added")
        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", catalog_seed.address(Ed.EVENTS)
        )
        expect(
            page.get_by_test_id("detail-panel").get_by_test_id("node-card")
        ).to_be_visible(timeout=15_000)

        state = catalog_api.state(draft_id)
        assert state["seq"] == 2, state["seq"]
        assert "ed_new" in _snapshot_names(state, "layers")
        assert catalog_seed.address(Ed.EVENTS) in _node_addresses(state)

    def test_rename_and_remove_empty_layer(
        self, page: Page, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)

        page.get_by_role("button", name="layer", exact=True).click()
        _prompt_name(page, "layer-name", "ed_tmp")
        page.get_by_role("button", name="rename layer ed_tmp").click()
        _prompt_name(page, "layer-name", "ed_tmp2")
        expect(page.locator('.pane__group[data-layer="ed_tmp2"]')).to_be_visible()

        page.get_by_role("button", name="remove layer ed_tmp2").click()
        expect(page.locator('.pane__group[data-layer="ed_tmp2"]')).to_have_count(0)

        names = _snapshot_names(catalog_api.state(draft_id), "layers")
        assert "ed_tmp" not in names
        assert "ed_tmp2" not in names


class TestDragAndDrop:
    def test_object_dragged_onto_a_lane_becomes_its_node(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Объект из дерева тащится на дорожку слоя ed_src и становится узлом
        этого слоя без диалога."""
        _open_draft(page, stand, draft_id)
        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        lane = page.locator(f'{Selector.LANE}[data-layer="{Ed.SRC}"]')

        _drag(page, source, lane, (20, 12))

        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible(timeout=LIVE_TIMEOUT_MS)
        expect(added.locator(".proc-node__layer")).to_have_text(Ed.SRC)
        lane_box = Css.box(lane)
        assert lane_box.contains(Css.box(added), slack=2)

        state = catalog_api.state(draft_id)
        node = next(
            node
            for node in state["snapshot"]["nodes"].values()
            if node["ref"]["path"][-1] == Ed.EVENTS
        )
        assert node["layer_id"] == catalog_seed.id_of(Ed.SRC)

    def test_object_dropped_off_the_lanes_asks_for_a_layer(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        _open_draft(page, stand, draft_id)
        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        canvas = page.get_by_test_id("canvas")

        _drag(page, source, canvas, (30, 30))

        prompt = _dialog(page, "drop-layer")
        expect(prompt).to_be_visible()
        prompt.get_by_label("layer for the dropped object").select_option(label=Ed.DST)
        prompt.get_by_role("button", name="add node").click()
        expect(prompt).to_have_count(0)

        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible(timeout=LIVE_TIMEOUT_MS)
        expect(added.locator(".proc-node__layer")).to_have_text(Ed.DST)
        assert catalog_seed.address(Ed.EVENTS) in _node_addresses(
            catalog_api.state(draft_id)
        )


class TestLanes:
    def test_isolated_node_stays_in_its_layer_lane(
        self, page: Page, stand: StandProcess, catalog_seed: Seed, draft_id: str
    ) -> None:
        """Узел без потоков (ed_returns) не выпадает в отдельную компоненту:
        дорожки слоёв не пересекаются, каждая карточка лежит в своей."""
        _open_draft(page, stand, draft_id)

        src = Css.box(page.locator(f'{Selector.LANE}[data-layer="{Ed.SRC}"]'))
        dst = Css.box(page.locator(f'{Selector.LANE}[data-layer="{Ed.DST}"]'))
        assert src.right <= dst.x + 1, f"lanes overlap: {src} vs {dst}"

        members = {**catalog_seed.tables, **catalog_seed.routines}
        for name, layer in members.items():
            node = Css.box(page.locator(catalog_seed.node(name)))
            lane = Css.box(page.locator(f'{Selector.LANE}[data-layer="{layer}"]'))
            assert lane.contains(node, slack=2), f"{name} is outside {layer}"


class TestNodePanel:
    def test_node_form_changes_alias_layer_and_note(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        _open_draft(page, stand, draft_id)
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_role("button", name="edit node").click()

        form = page.get_by_test_id("node-form")
        form.get_by_label("node alias").fill("ed_orders_v2")
        form.get_by_label("node layer").select_option(label=Ed.DST)
        form.get_by_label("node note").fill("moved to dst")
        form.get_by_role("button", name="save node").click()

        node = page.locator(catalog_seed.node(Ed.ORDERS))
        expect(node).to_have_attribute("data-label", "ed_orders_v2")
        expect(node).to_have_attribute("data-status", "modified")
        expect(node.locator(".proc-node__layer")).to_have_text(Ed.DST)
        expect(panel.get_by_test_id("panel-name").first).to_have_text("ed_orders_v2")
        expect(panel.get_by_test_id("panel-description")).to_contain_text(
            "moved to dst"
        )

        stored = catalog_api.state(draft_id)["snapshot"]["nodes"][
            catalog_seed.id_of(Ed.ORDERS)
        ]
        assert stored["alias"] == "ed_orders_v2"
        assert stored["layer_id"] == catalog_seed.id_of(Ed.DST)

    def test_retarget_points_the_node_at_another_object(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Перенацеливание: кнопка на панели узла переводит панель на дерево,
        выбранный объект получает кнопку «retarget … here», после неё узел
        стоит на новом адресе, его поток остаётся."""
        _open_draft(page, stand, draft_id)
        page.locator(catalog_seed.node(Ed.SALES)).click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_role("button", name="retarget node").click()
        expect(panel.locator('[data-notice="retarget-hint"]')).to_be_visible()
        expect(page.get_by_test_id("left-pane")).to_have_attribute(
            "data-tab", "sources"
        )

        objects = _pick_object(page, catalog_seed, Ed.EVENTS)
        objects.get_by_role("button", name=f"retarget {Ed.SALES} here").click()

        moved = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(moved).to_be_visible()
        expect(page.locator(catalog_seed.node(Ed.SALES))).to_have_count(0)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(1)
        expect(page.get_by_test_id("detail-panel")).to_have_attribute(
            "data-node", catalog_seed.address(Ed.EVENTS)
        )

        stored = catalog_api.state(draft_id)["snapshot"]["nodes"][
            catalog_seed.id_of(Ed.SALES)
        ]
        assert stored["ref"]["path"][-1] == Ed.EVENTS

    def test_remove_node_takes_its_flows_along(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        _open_draft(page, stand, draft_id)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(1)

        page.locator(catalog_seed.node(Ed.SALES)).click()
        page.get_by_test_id("detail-panel").get_by_role(
            "button", name="remove node"
        ).click()

        expect(page.locator(catalog_seed.node(Ed.SALES))).to_have_count(0)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(0)
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)

        state = catalog_api.state(draft_id)
        assert catalog_seed.address(Ed.SALES) not in _node_addresses(state)
        assert state["snapshot"]["flows"] == {}


class TestFlows:
    def test_flow_from_panel_then_removed_from_its_form(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Поток из панели узла: приёмник и вид в форме, колонки источника по
        именам; удаляется кнопкой формы."""
        _open_draft(page, stand, draft_id)
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name="flow", exact=True
        ).click()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        form.get_by_label("flow target").select_option(
            value=catalog_seed.id_of(Ed.RETURNS)
        )
        form.get_by_label("load kind").select_option(label=Ed.HASH)
        form.get_by_label(f"load field {Ed.HASH_FIELD}").select_option(value="id")
        form.get_by_role("button", name="save flow").click()

        expect(form).to_have_count(0)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(2)
        expect(
            page.locator(Selector.EDGE_LABEL).filter(has_text=Ed.HASH)
        ).to_have_count(1)

        flows = catalog_api.state(draft_id)["snapshot"]["flows"]
        assert len(flows) == 2
        hash_kind = catalog_seed.id_of(Ed.HASH)
        hashed = [
            flow for flow in flows.values() if flow["load"]["kind_id"] == hash_kind
        ]
        assert hashed[0]["load"]["values"] == {Ed.HASH_FIELD: ["id"]}

        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name=f"edit flow to {Ed.RETURNS}"
        ).click()
        page.get_by_test_id("flow-form").get_by_role(
            "button", name="remove flow"
        ).click()
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(1)
        assert len(catalog_api.state(draft_id)["snapshot"]["flows"]) == 1

    def test_connecting_nodes_on_the_canvas_opens_the_flow_form(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        _open_draft(page, stand, draft_id)
        source = page.locator(catalog_seed.node(Ed.ORDERS)).locator(
            ".react-flow__handle.source"
        )
        target = page.locator(catalog_seed.node(Ed.RETURNS)).locator(
            ".react-flow__handle.target"
        )

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
        expect(form.locator("p.note")).to_have_text(f"{Ed.ORDERS} → {Ed.RETURNS}")
        form.get_by_label("load kind").select_option(label=Ed.FULL)
        form.get_by_role("button", name="save flow").click()

        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(2)
        assert len(catalog_api.state(draft_id)["snapshot"]["flows"]) == 2

    def test_clicking_an_edge_edits_its_flow(
        self, page: Page, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        page.locator(Selector.EDGE_LABEL).first.click()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        form.get_by_label("flow description").fill("nightly full copy")
        form.get_by_role("button", name="save flow").click()
        expect(form).to_have_count(0)
        _landed(page, 1)

        flows = list(catalog_api.state(draft_id)["snapshot"]["flows"].values())
        assert flows[0]["description"] == "nightly full copy"


class TestLive:
    def test_foreign_portion_shows_up_and_own_edit_lands_on_top(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Порция, добавленная мимо страницы, появляется без перезагрузки; своя
        правка после неё ложится поверх, а не затирает."""
        _open_draft(page, stand, draft_id)

        catalog_api.append(draft_id, [catalog_seed.node_op(Ed.EVENTS, Ed.DST)])
        expect(page.locator(catalog_seed.node(Ed.EVENTS))).to_be_visible(
            timeout=LIVE_TIMEOUT_MS
        )

        page.locator(catalog_seed.node(Ed.SALES)).click()
        page.get_by_test_id("detail-panel").get_by_role(
            "button", name="edit node"
        ).click()
        form = page.get_by_test_id("node-form")
        form.get_by_label("node alias").fill("ed_sales_v2")
        form.get_by_role("button", name="save node").click()
        expect(page.locator(catalog_seed.node(Ed.SALES))).to_have_attribute(
            "data-label", "ed_sales_v2"
        )

        state = catalog_api.state(draft_id)
        assert state["seq"] == 2
        assert catalog_seed.address(Ed.EVENTS) in _node_addresses(state)
        assert (
            state["snapshot"]["nodes"][catalog_seed.id_of(Ed.SALES)]["alias"]
            == "ed_sales_v2"
        )

    def test_publish_conflict_offers_rebase_then_publishes(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Пока черновик открыт, публикуется другой: страница показывает кнопку
        обновления, публикация упирается в конфликт, перебазирование снимает его."""
        _open_draft(page, stand, draft_id)
        panel = _pick_object(page, catalog_seed, Ed.EVENTS)
        panel.get_by_label("layer for the new node").select_option(label=Ed.DST)
        panel.get_by_role("button", name="add to layer").click()
        expect(page.locator(catalog_seed.node(Ed.EVENTS))).to_be_visible()
        expect(page.get_by_test_id("rebase-button")).to_have_count(0)

        version = catalog_api.publish_ops(
            "edit other",
            [
                {
                    "op": "add_layer",
                    "layer": {
                        "id": str(UUID(int=0xE0F1)),
                        "name": "ed_other",
                        "position": 9,
                        "description": "",
                    },
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
        page.get_by_test_id("left-pane").get_by_role("tab", name="process").click()
        expect(page.locator('.pane__group[data-layer="ed_other"]')).to_be_visible()
        expect(page.locator(catalog_seed.node(Ed.EVENTS))).to_be_visible()

        page.get_by_test_id("publish-button").click()
        expect(page.locator('[data-notice="draft-closed"]')).to_be_visible()
        expect(page.locator('[data-testid="catalog-page"]')).to_have_attribute(
            "data-editable", "false"
        )

        assert catalog_seed.address(Ed.EVENTS) in catalog_api.node_addresses()
        assert catalog_api.state(draft_id)["draft"]["status"] == "published"


class TestStaleness:
    def test_new_source_version_marks_nodes_stale_and_pins_are_raised(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Источник получает версию без ed_returns: узел помечен устаревшим по
        событию, панель называет причину, «raise pins» поднимает привязки и
        перечисляет, что перестало сходиться; перенацеливание узла на живой
        объект снимает устаревание."""
        _open_draft(page, stand, draft_id)
        catalog = page.get_by_test_id("catalog-page")
        expect(catalog).to_have_attribute("data-stale", "0")

        tables = [name for name in catalog_seed.tables if name != Ed.RETURNS]
        version = catalog_seed.next_version([*tables, Ed.EVENTS, Ed.ARCHIVE])
        assert version == 2

        stale = page.locator(catalog_seed.node(Ed.RETURNS))
        expect(stale).to_have_attribute("data-stale", "true", timeout=LIVE_TIMEOUT_MS)
        expect(catalog).to_have_attribute("data-stale", "1")
        expect(page.get_by_test_id("stale-chip")).to_have_text("1 stale")

        stale.click()
        reasons = page.get_by_test_id("detail-panel").get_by_test_id("detail-stale")
        expect(reasons.locator('[data-reason="object_removed"]')).to_have_count(1)
        expect(reasons).to_contain_text("v1 → v2")

        page.get_by_test_id("bump-pins-button").click()
        toast = page.locator('.toast[data-tone="error"]').first
        expect(toast).to_contain_text("pins raised", timeout=LIVE_TIMEOUT_MS)
        expect(toast).to_contain_text("missing object")
        expect(page.get_by_test_id("bump-pins-button")).to_have_count(
            0, timeout=LIVE_TIMEOUT_MS
        )
        assert catalog_api.state(draft_id)["draft"]["pins"] == {
            catalog_seed.source_id: 2
        }

        page.get_by_test_id("detail-panel").get_by_role(
            "button", name="retarget node"
        ).click()
        objects = _pick_object(page, catalog_seed, Ed.ARCHIVE)
        objects.get_by_role("button", name=f"retarget {Ed.RETURNS} here").click()
        expect(page.locator(catalog_seed.node(Ed.ARCHIVE))).to_be_visible()
        expect(catalog).to_have_attribute("data-stale", "0")
