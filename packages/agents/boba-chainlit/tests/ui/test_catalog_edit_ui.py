"""Правки черновика на странице процесса: слой через подсказку имени, узел из
дерева подключения кнопкой и перетаскиванием, форма узла, перенацеливание,
поток из панели и соединением на холсте с парами колонок, удаление, чужие
порции и новые версии по событиям, устаревание после новой версии снимка и
поднятие привязок, публикация и перебазирование устаревшего черновика.

Модуль сеет свой процесс ed_process над подключением ed_prod и на выходе
удаляет его вместе с подключением.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from catalog_ui import Api, Ed, Seed, Selector, settled_box
from playwright.sync_api import Browser, FloatRect, Locator, Page, ViewportSize, expect

from boba.stand.ui.look import Box, Css, Tokens
from boba.stand.ui.stand import REPO_ROOT, StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1400, "height": 900}
EDITABLE = f'{Selector.PAGE}[data-editable="true"]'
LIVE_TIMEOUT_MS = 15_000
TOKENS_CSS = (
    REPO_ROOT / "packages/agents/boba-chainlit/web/catalog/src/styles/tokens.css"
)


@pytest.fixture
def draft_id(
    catalog_api: Api, catalog_seed: Seed, request: pytest.FixtureRequest
) -> Iterator[str]:
    created = catalog_api.new_draft(
        catalog_seed.process_id, f"edit {request.node.name}"
    )
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


def _open_draft(
    page: Page, stand: StandProcess, draft_id: str, query: str = ""
) -> None:
    page.goto(f"{stand.config.base_url}/catalog/drafts/{draft_id}{query}")
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
    """Вкладка подключений: раскрыть ed_prod, базу, схему public и группу
    таблиц; вернуть панель."""
    pane = page.get_by_test_id("left-pane")
    pane.get_by_role("tab", name="connections").click()
    branch = pane.locator(
        f'[data-testid="connection-branch"][data-connection="{seed.connection_name}"]'
    )
    branch.get_by_role(
        "button", name=f"expand connection {seed.connection_name}"
    ).click()

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


def _node_handle(page: Page, seed: Seed, name: str, kind: str) -> Locator:
    """Ручка карточки целиком: линия от неё открывает форму потока."""
    return page.locator(seed.node(name)).locator(
        f'.react-flow__handle.{kind}[data-handleid="__node"]'
    )


def _column_handle(
    page: Page, seed: Seed, name: str, column: str, kind: str
) -> Locator:
    return (
        page.locator(seed.node(name))
        .locator(f'[data-column="{column}"]')
        .locator(f".react-flow__handle.{kind}")
    )


def _column(card: Locator, name: str) -> Locator:
    return card.locator(f'[data-column="{name}"]')


def _connect(page: Page, source: Locator, target: Locator) -> None:
    """Линия мышью от одной ручки к другой; ручки колонок видны при наведении."""
    source.locator("..").hover()
    start = source.bounding_box()
    assert start is not None
    page.mouse.move(*_centre(start))
    page.mouse.down()
    target.locator("..").hover()
    end = target.bounding_box()
    assert end is not None
    page.mouse.move(*_centre(end), steps=12)
    page.mouse.up()


def _click_line(page: Page, edge_id: str) -> None:
    """Выбор линии кликом по её широкой зоне; линия между колонками одной
    высоты прямая, центр рамки лежит на ней."""
    line = page.locator(f'.react-flow__edge[data-id="{edge_id}"]').locator(
        ".react-flow__edge-interaction"
    )
    box = line.bounding_box()
    assert box is not None
    page.mouse.click(*_centre(box))
    expect(page.locator(f'.react-flow__edge[data-id="{edge_id}"]')).to_have_class(
        re.compile(r"\bselected\b")
    )


def _contains(outer: FloatRect, inner: Box, slack: float = 2) -> bool:
    return (
        outer["x"] - slack <= inner.x
        and outer["y"] - slack <= inner.y
        and outer["x"] + outer["width"] + slack >= inner.right
        and outer["y"] + outer["height"] + slack >= inner.bottom
    )


def _frame(page: Page, name: str) -> Locator:
    return page.locator(f'{Selector.FRAME}[data-group="{name}"]')


def _stored_node(
    catalog_api: Api, draft_id: str, seed: Seed, name: str
) -> dict[str, Any]:
    """Узел черновика по адресу объекта: id у добавленных со страницы свой."""
    for node in catalog_api.state(draft_id)["snapshot"]["nodes"].values():
        if "/".join(node["ref"]["path"]) == seed.address(name):
            return dict(node)

    raise AssertionError(f"no node over {seed.address(name)} in draft {draft_id}")


def _drag_card(page: Page, card: Locator, to: tuple[float, float]) -> None:
    """Перетаскивание карточки за шапку в точку экрана."""
    box = card.locator(".proc-node__header").bounding_box()
    assert box is not None
    page.mouse.move(*_centre(box))
    page.mouse.down()
    page.mouse.move(to[0], to[1], steps=12)
    page.mouse.up()


class TestGroups:
    def test_group_is_added_empty_even_with_a_card_selected(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Объект из дерева подключения тащится на холст мимо рамок и встаёт
        узлом без группы; «group» ставит на холст пустую рамку, не трогая
        карточки — даже выбранную."""
        _open_draft(page, stand, draft_id)

        panel = _pick_object(page, catalog_seed, Ed.EVENTS)
        expect(panel).to_have_attribute("data-in-process", "false")
        expect(panel.get_by_role("button", name="add to the canvas")).to_have_count(0)
        source = page.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        canvas = page.get_by_test_id("canvas")
        box = Css.box(canvas)
        _drag(page, source, canvas, (box.width - 60, box.height - 60))

        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible(timeout=LIVE_TIMEOUT_MS)
        expect(added).to_have_attribute("data-status", "added")
        expect(added.locator(".proc-node__group")).to_have_text("—")
        # панель узла после броска не открывается
        expect(page.get_by_test_id("detail-panel")).to_have_count(0)
        _landed(page, 1)

        added.click()
        group = page.get_by_test_id("group-button")
        expect(group).to_have_text("group")
        expect(group).to_be_enabled()
        group.click()
        _prompt_name(page, "group-name", "ed_new")
        _landed(page, 2)

        frame = _frame(page, "ed_new")
        expect(frame).to_be_visible()
        expect(frame.locator(".group-frame__count")).to_have_text("0")
        expect(added.locator(".proc-node__group")).to_have_text("—")
        state = catalog_api.state(draft_id)
        assert "ed_new" in _snapshot_names(state, "groups")
        assert catalog_seed.address(Ed.EVENTS) in _node_addresses(state)
        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.EVENTS)
        assert stored["group_id"] is None

    def test_card_dragged_into_a_frame_joins_the_group_with_a_highlight(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Карточка входит в группу, когда её отпускают над рамкой; пока
        тащат, рамка и карточка подсвечены цветом done."""
        tokens = Tokens.load(TOKENS_CSS)
        _open_draft(page, stand, draft_id)
        page.get_by_test_id("group-button").click()
        _prompt_name(page, "group-name", "ed_new")
        _landed(page, 1)
        frame = _frame(page, "ed_new")
        canvas = page.get_by_test_id("canvas")
        loader = page.locator(catalog_seed.node(Ed.LOADER))

        target = settled_box(page, frame)
        header = loader.locator(".proc-node__header").bounding_box()
        assert header is not None
        page.mouse.move(*_centre(header))
        page.mouse.down()
        page.mouse.move(target["x"] + 60, target["y"] + 60, steps=12)
        expect(frame).to_have_attribute("data-drop", "true")
        expect(canvas).to_have_attribute("data-drop", "true")
        expect(frame).to_have_css("border-color", tokens.rgb("done"))
        expect(loader).to_have_css("border-color", tokens.rgb("done"))
        page.mouse.up()

        _landed(page, 2)
        expect(frame).to_have_attribute("data-drop", "false")
        expect(canvas).to_have_attribute("data-drop", "false")
        expect(frame.locator(".group-frame__count")).to_have_text("1")
        expect(loader.locator(".proc-node__group")).to_have_text("ed_new")
        assert _contains(settled_box(page, frame), Css.box(loader))
        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.LOADER)
        assert stored["group_id"] is not None

    def test_empty_frame_is_dragged_and_takes_a_dropped_object(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Пустая рамка — отдельный предмет на холсте: её можно оттащить, и
        объект из дерева, брошенный в неё на новом месте, входит в группу."""
        _open_draft(page, stand, draft_id)
        page.get_by_test_id("group-button").click()
        _prompt_name(page, "group-name", "ed_empty")
        _landed(page, 1)

        frame = _frame(page, "ed_empty")
        before = settled_box(page, frame)
        title = frame.locator(".group-frame__title").bounding_box()
        assert title is not None
        page.mouse.move(*_centre(title))
        page.mouse.down()
        page.mouse.move(title["x"] + 40, title["y"] + 160, steps=12)
        page.mouse.up()
        after = settled_box(page, frame)
        assert after["y"] > before["y"] + 100, (before, after)
        # сдвиг рамки — вид страницы, не порция черновика
        assert catalog_api.state(draft_id)["seq"] == 1

        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        _drag(page, source, frame, (30, 50))
        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible(timeout=LIVE_TIMEOUT_MS)
        expect(added.locator(".proc-node__group")).to_have_text("ed_empty")
        expect(frame.locator(".group-frame__count")).to_have_text("1")

    def test_rename_group_and_remove_it_keeping_the_nodes(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Карандаш на рамке переименовывает группу; корзина снимает группу, а
        её карточки остаются на холсте без группы."""
        _open_draft(page, stand, draft_id)

        _frame(page, Ed.SRC).get_by_role(
            "button", name=f"rename group {Ed.SRC}"
        ).click()
        _prompt_name(page, "group-name", "ed_tmp2")
        expect(_frame(page, "ed_tmp2")).to_be_visible()
        expect(_frame(page, Ed.SRC)).to_have_count(0)

        _frame(page, "ed_tmp2").get_by_role(
            "button", name="remove group ed_tmp2"
        ).click()
        expect(_frame(page, "ed_tmp2")).to_have_count(0)
        orders = page.locator(catalog_seed.node(Ed.ORDERS))
        expect(orders).to_be_visible()
        expect(orders.locator(".proc-node__group")).to_have_text("—")

        state = catalog_api.state(draft_id)
        names = _snapshot_names(state, "groups")
        assert Ed.SRC not in names
        assert "ed_tmp2" not in names
        assert (
            _stored_node(catalog_api, draft_id, catalog_seed, Ed.ORDERS)["group_id"]
            is None
        )


class TestDragAndDrop:
    def test_object_dropped_into_a_frame_joins_the_group(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Объект из дерева падает в рамку ed_src: узел встаёт туда, где его
        отпустили, и сразу входит в группу, без диалога."""
        _open_draft(page, stand, draft_id)
        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        frame = _frame(page, Ed.SRC)

        _drag(page, source, frame, (20, 40))

        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible(timeout=LIVE_TIMEOUT_MS)
        expect(added.locator(".proc-node__group")).to_have_text(Ed.SRC)
        assert Css.box(frame).contains(Css.box(added), slack=2)

        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.EVENTS)
        assert stored["group_id"] == catalog_seed.id_of(Ed.SRC)
        assert stored["position"] is not None

    def test_object_dropped_off_the_frames_stands_alone(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Объект брошен мимо рамок: узел без группы там, где отпущен."""
        _open_draft(page, stand, draft_id)
        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        canvas = page.get_by_test_id("canvas")
        box = Css.box(canvas)

        _drag(page, source, canvas, (box.width - 60, box.height - 60))

        added = page.locator(catalog_seed.node(Ed.EVENTS))
        expect(added).to_be_visible(timeout=LIVE_TIMEOUT_MS)
        expect(added.locator(".proc-node__group")).to_have_text("—")
        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.EVENTS)
        assert stored["group_id"] is None
        assert stored["position"] is not None

    def test_dragging_a_card_moves_it_between_groups(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Карточка ed_returns перетаскивается из рамки ed_dst в рамку ed_src и
        обратно: позиция и группа уходят в черновик одной порцией."""
        _open_draft(page, stand, draft_id)
        returns = page.locator(catalog_seed.node(Ed.RETURNS))
        before = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        target = Css.box(_frame(page, Ed.SRC))

        _drag_card(page, returns, (target.x + 40, target.bottom - 40))

        _landed(page, 1)
        expect(returns.locator(".proc-node__group")).to_have_text(Ed.SRC)
        after = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert after["group_id"] == catalog_seed.id_of(Ed.SRC)
        assert after["position"] != before["position"]

        back = Css.box(_frame(page, Ed.DST))
        _drag_card(page, returns, (back.x + 40, back.bottom - 40))

        _landed(page, 2)
        expect(returns.locator(".proc-node__group")).to_have_text(Ed.DST)
        moved = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert moved["group_id"] == catalog_seed.id_of(Ed.DST)

    def test_dragging_a_card_out_of_its_frame_keeps_the_group(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Карточка, вытащенная из рамки на пустое место, группу не теряет:
        рамка растягивается за ней; снять группу можно только формой узла."""
        _open_draft(page, stand, draft_id)
        returns = page.locator(catalog_seed.node(Ed.RETURNS))
        canvas = Css.box(page.get_by_test_id("canvas"))
        frame = _frame(page, Ed.DST)
        settled_box(page, frame)

        _drag_card(
            page, returns, (canvas.x + canvas.width - 80, canvas.y + canvas.height - 80)
        )

        _landed(page, 1)
        expect(returns.locator(".proc-node__group")).to_have_text(Ed.DST)
        after = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert after["group_id"] == catalog_seed.id_of(Ed.DST)
        assert _contains(settled_box(page, frame), Css.box(returns))

        returns.click()
        panel = page.get_by_test_id("detail-panel")
        panel.get_by_role("button", name="edit node").click()
        form = panel.get_by_test_id("node-form")
        form.get_by_label("node group").select_option("")
        form.get_by_role("button", name="save node").click()
        _landed(page, 2)
        expect(returns.locator(".proc-node__group")).to_have_text("—")
        freed = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert freed["group_id"] is None


class TestFrames:
    def test_frames_wrap_their_nodes_and_loose_nodes_stay_outside(
        self, page: Page, stand: StandProcess, catalog_seed: Seed, draft_id: str
    ) -> None:
        """Рамка каждой группы обнимает свои карточки; процедура без группы
        стоит вне рамок; рамки не пересекаются."""
        _open_draft(page, stand, draft_id)

        src = Css.box(_frame(page, Ed.SRC))
        dst = Css.box(_frame(page, Ed.DST))
        assert src.right <= dst.x + 1 or dst.right <= src.x + 1, (
            f"frames overlap: {src} vs {dst}"
        )

        for name, group in catalog_seed.tables.items():
            node = Css.box(page.locator(catalog_seed.node(name)))
            frame = Css.box(_frame(page, group))
            assert frame.contains(node, slack=2), f"{name} is outside {group}"

        loader = Css.box(page.locator(catalog_seed.node(Ed.LOADER)))
        assert not src.contains(loader, slack=0)
        assert not dst.contains(loader, slack=0)


class TestNodePanel:
    def test_node_form_changes_alias_group_and_note(
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
        form.get_by_label("node group").select_option(label=Ed.DST)
        form.get_by_label("node note").fill("moved to dst")
        form.get_by_role("button", name="save node").click()

        node = page.locator(catalog_seed.node(Ed.ORDERS))
        expect(node).to_have_attribute("data-label", "ed_orders_v2")
        expect(node).to_have_attribute("data-status", "modified")
        expect(node.locator(".proc-node__group")).to_have_text(Ed.DST)
        expect(panel.get_by_test_id("panel-name").first).to_have_text("ed_orders_v2")
        expect(panel.get_by_test_id("panel-description")).to_contain_text(
            "moved to dst"
        )

        stored = catalog_api.state(draft_id)["snapshot"]["nodes"][
            catalog_seed.id_of(Ed.ORDERS)
        ]
        assert stored["alias"] == "ed_orders_v2"
        assert stored["group_id"] == catalog_seed.id_of(Ed.DST)

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
            "data-tab", "connections"
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

    def test_card_is_resized_by_its_edges_and_the_width_is_kept(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Карточка тянется за правый и левый край у шапки (вдоль колонок край
        держат их ручки): ширина уходит в черновик (за левый край — вместе с
        позицией) и переживает перезагрузку; ручки колонок следуют за новой
        шириной, уже минимума не бывает."""
        _open_draft(page, stand, draft_id)
        returns = page.locator(catalog_seed.node(Ed.RETURNS))
        expect(returns).to_have_attribute("data-resizable", "true")
        before = settled_box(page, returns)
        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert stored["width"] is None

        right = returns.get_by_test_id("resize-right").bounding_box()
        assert right is not None
        page.mouse.move(right["x"] + right["width"] / 2, right["y"] + 16)
        page.mouse.down()
        page.mouse.move(right["x"] + 120, right["y"] + 16, steps=10)
        page.mouse.up()

        _landed(page, 1)
        wider = settled_box(page, returns)
        assert wider["width"] > before["width"] + 100, (before, wider)
        assert abs(wider["x"] - before["x"]) <= 2
        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert stored["width"] is not None
        assert stored["width"] > 240
        # ручка колонки id стоит у нового правого края
        handle = returns.locator('[data-column="id"] .react-flow__handle.source')
        assert abs(Css.box(handle).x - wider["x"] - wider["width"]) <= 8

        page.reload()
        page.wait_for_selector(Selector.READY, timeout=30_000)
        kept = settled_box(page, returns)
        assert abs(kept["width"] - wider["width"]) <= 2, (wider, kept)

        left = returns.get_by_test_id("resize-left").bounding_box()
        assert left is not None
        page.mouse.move(left["x"] + left["width"] / 2, left["y"] + 16)
        page.mouse.down()
        page.mouse.move(left["x"] + 600, left["y"] + 16, steps=10)
        page.mouse.up()

        _landed(page, 2)
        narrow = settled_box(page, returns)
        assert narrow["width"] < kept["width"], (kept, narrow)
        assert narrow["x"] > kept["x"]
        stored = _stored_node(catalog_api, draft_id, catalog_seed, Ed.RETURNS)
        assert stored["width"] == 160
        assert stored["position"]["x"] > 0

    def test_moving_a_card_does_not_reload_its_panel(
        self,
        page: Page,
        stand: StandProcess,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Сдвиг открытой карточки не перечитывает карточку объекта в панели:
        запрос к объекту уходит один раз, секция «in the database» остаётся
        на месте, а не мигает загрузкой."""
        _open_draft(page, stand, draft_id)
        returns = page.locator(catalog_seed.node(Ed.RETURNS))
        returns.click()
        panel = page.get_by_test_id("detail-panel")
        expect(panel.get_by_test_id("node-card")).to_be_visible(timeout=LIVE_TIMEOUT_MS)

        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        canvas = Css.box(page.get_by_test_id("canvas"))
        _drag_card(page, returns, (canvas.x + canvas.width - 120, canvas.y + 120))
        _landed(page, 1)
        _drag_card(page, returns, (canvas.x + canvas.width - 160, canvas.y + 220))
        _landed(page, 2)

        expect(panel.get_by_test_id("node-card")).to_be_visible()
        expect(panel).to_have_attribute("data-node", catalog_seed.address(Ed.RETURNS))
        objects = [url for url in requests if "/object" in url]
        assert objects == [], objects

    def test_drop_from_the_connections_tab_keeps_the_tab_after_the_draft_starts(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
    ) -> None:
        """Объект из дерева брошен на холст опубликованного процесса: первая
        правка заводит черновик и уводит на него, но вкладка подключений и
        остальной адрес остаются — дальше можно тащить следующие таблицы."""
        before = {draft["id"] for draft in catalog_api.my_drafts()}
        page.goto(
            f"{stand.config.base_url}/catalog/processes/{catalog_seed.process_id}"
            "?pane=connections&mode=ALL_FIELDS"
        )
        page.wait_for_selector(Selector.READY, timeout=30_000)
        page.wait_for_selector(EDITABLE, timeout=30_000)
        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        canvas = page.get_by_test_id("canvas")
        box = Css.box(canvas)

        _drag(page, source, canvas, (box.width - 60, box.height - 60))

        page.wait_for_url(
            re.compile(r"/catalog/drafts/[0-9a-f-]{36}\?"), timeout=30_000
        )
        created = {draft["id"] for draft in catalog_api.my_drafts()} - before
        try:
            assert "pane=connections" in page.url
            assert "mode=ALL_FIELDS" in page.url
            expect(page.get_by_test_id("left-pane")).to_have_attribute(
                "data-tab", "connections"
            )
            expect(page.locator(catalog_seed.node(Ed.EVENTS))).to_be_visible(
                timeout=LIVE_TIMEOUT_MS
            )
        finally:
            for draft_id in created:
                catalog_api.discard(draft_id)


class TestFlows:
    def test_flow_from_panel_then_removed_from_its_form(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Поток из панели узла: приёмник в форме, пара колонок по именам из
        привязанной версии, повтор пары отвергается; удаляется кнопкой формы."""
        _open_draft(page, stand, draft_id)
        page.locator(catalog_seed.node(Ed.ORDERS)).click()
        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name="flow", exact=True
        ).click()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        save = form.get_by_role("button", name="save flow")
        expect(save).to_be_disabled()
        form.get_by_label("flow target").select_option(
            value=catalog_seed.id_of(Ed.RETURNS)
        )
        form.get_by_test_id("add-pair").click()
        expect(save).to_be_disabled()
        form.get_by_label("from column 0").select_option(value="id")
        form.get_by_label("to column 0").select_option(value="id")
        expect(save).to_be_enabled()

        form.get_by_test_id("add-pair").click()
        form.get_by_label("from column 1").select_option(value="id")
        form.get_by_label("to column 1").select_option(value="id")
        expect(form.get_by_test_id("flow-repeated")).to_contain_text("id → id")
        expect(save).to_be_disabled()
        form.get_by_role("button", name="remove pair 1").click()
        expect(form.get_by_test_id("flow-repeated")).to_have_count(0)
        save.click()

        expect(form).to_have_count(0)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(2)
        expect(
            page.locator(Selector.EDGE_LABEL).filter(has_text="1 col")
        ).to_have_count(1)

        flows = catalog_api.state(draft_id)["snapshot"]["flows"]
        assert len(flows) == 2
        returns = catalog_seed.id_of(Ed.RETURNS)
        added = [flow for flow in flows.values() if flow["to_node_id"] == returns]
        assert added[0]["columns"] == [{"from_column": "id", "to_column": "id"}]

        page.get_by_test_id("detail-outgoing").get_by_role(
            "button", name=f"edit flow to {Ed.RETURNS}"
        ).click()
        edit = page.get_by_test_id("flow-form")
        expect(edit.locator("tbody tr")).to_have_count(1)
        edit.get_by_role("button", name="remove flow").click()

        expect(edit).to_have_count(0)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(1)
        expect(
            page.get_by_test_id("detail-outgoing").get_by_role(
                "button", name=f"edit flow to {Ed.RETURNS}"
            )
        ).to_have_count(0)
        assert len(catalog_api.state(draft_id)["snapshot"]["flows"]) == 1

    def test_connecting_nodes_on_the_canvas_opens_the_flow_form(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """В режиме имён колонок на карточках нет, и линия от ручки карточки к
        ручке карточки открывает форму потока с выбором пар; при видимых
        колонках ручки карточки целиком линий не принимают."""
        _open_draft(page, stand, draft_id)
        whole = _node_handle(page, catalog_seed, Ed.ORDERS, "source")
        expect(whole).not_to_have_class(re.compile(r"\bconnectable\b"))

        _open_draft(page, stand, draft_id, "?mode=TABLE_NAME")
        expect(whole).to_have_class(re.compile(r"\bconnectable\b"))
        _connect(
            page,
            _node_handle(page, catalog_seed, Ed.ORDERS, "source"),
            _node_handle(page, catalog_seed, Ed.RETURNS, "target"),
        )

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        expect(form.locator("p.note")).to_have_text(f"{Ed.ORDERS} → {Ed.RETURNS}")
        # «match by name» подбирает пары по одинаковым именам с обеих сторон
        form.get_by_test_id("match-by-name").click()
        expect(form.locator("tbody tr")).to_have_count(3)
        form.get_by_role("button", name="save flow").click()

        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(2)
        expect(
            page.locator(Selector.EDGE_LABEL).filter(has_text="3 cols")
        ).to_have_count(1)
        flows = catalog_api.state(draft_id)["snapshot"]["flows"]
        assert len(flows) == 2
        returns = catalog_seed.id_of(Ed.RETURNS)
        matched = [flow for flow in flows.values() if flow["to_node_id"] == returns]
        assert [c["from_column"] for c in matched[0]["columns"]] == [
            "id",
            "name",
            "updated_at",
        ]

    def test_double_click_on_a_line_edits_its_flow(
        self, page: Page, stand: StandProcess, catalog_api: Api, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        page.locator(Selector.EDGE_LABEL).first.dblclick()

        form = page.get_by_test_id("flow-form")
        expect(form).to_be_visible()
        form.get_by_label("flow description").fill("nightly full copy")
        form.get_by_role("button", name="save flow").click()
        expect(form).to_have_count(0)
        _landed(page, 1)

        flows = list(catalog_api.state(draft_id)["snapshot"]["flows"].values())
        assert flows[0]["description"] == "nightly full copy"


class TestColumnLines:
    """Потоки линиями между колонками: линия от колонки к колонке добавляет
    пару без формы, три колонки сходятся в одну, подсветка колонок по линиям,
    повтор пары отвергается, линия снимается крестиком и клавишей Delete."""

    def test_line_from_column_to_column_adds_a_pair(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        # все колонки: updated_at не ключ и не участник потока
        _open_draft(page, stand, draft_id, "?mode=ALL_FIELDS")
        # посеянный поток orders → sales с парами id→id и name→name: две линии
        expect(page.locator(".react-flow__edge")).to_have_count(2)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_text("2 cols")

        # посеянная пара ещё раз — отказ тостом, порций нет
        _connect(
            page,
            _column_handle(page, catalog_seed, Ed.ORDERS, "name", "source"),
            _column_handle(page, catalog_seed, Ed.SALES, "name", "target"),
        )
        expect(page.locator('.toast[data-tone="error"]')).to_contain_text(
            "name → name is already in the flow"
        )
        assert catalog_api.state(draft_id)["seq"] == 0

        _connect(
            page,
            _column_handle(page, catalog_seed, Ed.ORDERS, "updated_at", "source"),
            _column_handle(page, catalog_seed, Ed.SALES, "name", "target"),
        )
        expect(page.get_by_test_id("flow-form")).to_have_count(0)
        _landed(page, 1)
        expect(page.locator(".react-flow__edge")).to_have_count(3)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_text("3 cols")

        flow = next(iter(catalog_api.state(draft_id)["snapshot"]["flows"].values()))
        assert {(c["from_column"], c["to_column"]) for c in flow["columns"]} == {
            ("id", "id"),
            ("name", "name"),
            ("updated_at", "name"),
        }

        # вторая линия после перестройки холста: ещё одна пара
        _connect(
            page,
            _column_handle(page, catalog_seed, Ed.ORDERS, "updated_at", "source"),
            _column_handle(page, catalog_seed, Ed.SALES, "id", "target"),
        )
        _landed(page, 2)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_text("4 cols")

        # та же пара ещё раз — отказ тостом, порций не прибавилось
        _connect(
            page,
            _column_handle(page, catalog_seed, Ed.ORDERS, "updated_at", "source"),
            _column_handle(page, catalog_seed, Ed.SALES, "name", "target"),
        )
        expect(
            page.locator('.toast[data-tone="error"]').filter(has_text="updated_at")
        ).to_contain_text("updated_at → name is already in the flow")
        assert catalog_api.state(draft_id)["seq"] == 2

    def test_line_between_new_cards_starts_a_flow(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        _open_draft(page, stand, draft_id)
        _connect(
            page,
            _column_handle(page, catalog_seed, Ed.ORDERS, "id", "source"),
            _column_handle(page, catalog_seed, Ed.RETURNS, "id", "target"),
        )
        _landed(page, 1)
        expect(page.locator(Selector.EDGE_LABEL)).to_have_count(2)
        expect(
            page.locator(Selector.EDGE_LABEL).filter(has_text="1 col")
        ).to_have_count(1)

        flows = catalog_api.state(draft_id)["snapshot"]["flows"].values()
        returns = catalog_seed.id_of(Ed.RETURNS)
        added = [flow for flow in flows if flow["to_node_id"] == returns]
        assert added[0]["columns"] == [{"from_column": "id", "to_column": "id"}]

    def test_line_snaps_to_the_nearest_handle(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Линию не нужно целить: отпущенная в тридцати пикселях от ручки колонки
        она притягивается к ней, а ручка под линией подсвечена и увеличена."""
        _open_draft(page, stand, draft_id)
        source = _column_handle(page, catalog_seed, Ed.ORDERS, "id", "source")
        target = _column_handle(page, catalog_seed, Ed.RETURNS, "id", "target")
        source.locator("..").hover()
        start = source.bounding_box()
        assert start is not None
        page.mouse.move(*_centre(start))
        page.mouse.down()
        target.locator("..").hover()
        end = target.bounding_box()
        assert end is not None
        # отпускаем правее и ниже ручки, в радиусе притяжения
        page.mouse.move(end["x"] + 30, end["y"] + 24, steps=12)
        expect(target).to_have_class(re.compile(r"\bconnectingto\b"))
        page.mouse.up()

        _landed(page, 1)
        returns = catalog_seed.id_of(Ed.RETURNS)
        flows = catalog_api.state(draft_id)["snapshot"]["flows"].values()
        added = [flow for flow in flows if flow["to_node_id"] == returns]
        assert added[0]["columns"] == [{"from_column": "id", "to_column": "id"}]

    def test_hover_lights_the_lines_and_their_columns(
        self, page: Page, stand: StandProcess, catalog_seed: Seed, draft_id: str
    ) -> None:
        _open_draft(page, stand, draft_id)
        orders = page.locator(catalog_seed.node(Ed.ORDERS))
        sales = page.locator(catalog_seed.node(Ed.SALES))

        orders.locator(".proc-node__header").hover()
        expect(sales).to_have_attribute("data-highlighted", "true")
        expect(orders.locator('[data-column="id"]')).to_have_attribute(
            "data-lit", "true"
        )
        expect(orders.locator('[data-column="name"]')).to_have_attribute(
            "data-lit", "true"
        )
        # режим ключей: колонка вне потоков и не ключ не показана
        expect(orders.locator('[data-column="updated_at"]')).to_have_count(0)
        expect(sales.locator('[data-column="id"]')).to_have_attribute(
            "data-lit", "true"
        )
        expect(page.locator(".flow-edge--lit")).to_have_count(2)
        expect(page.locator(".flow-edge__particle")).to_have_count(6)

        page.mouse.move(5, 5)
        expect(page.locator(".flow-edge--lit")).to_have_count(0)
        expect(orders.locator('[data-column="id"]')).to_have_attribute(
            "data-lit", "false"
        )

    def test_selected_line_is_coloured_and_removed_by_delete_key(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Клик по линии выбирает её: линия и стрелка цвета ember, крестика
        нет; Delete снимает выбранную пару, последняя пара уносит поток."""
        tokens = Tokens.load(TOKENS_CSS)
        _open_draft(page, stand, draft_id)
        flow_id = next(iter(catalog_api.state(draft_id)["snapshot"]["flows"]))

        _click_line(page, f"{flow_id}#1")
        selected = page.locator(f'.react-flow__edge[data-id="{flow_id}#1"]')
        expect(selected.locator(".react-flow__edge-path")).to_have_css(
            "stroke", tokens.rgb("ember")
        )
        expect(
            page.get_by_role("button", name=re.compile("^remove pair"))
        ).to_have_count(0)
        other = page.locator(f'.react-flow__edge[data-id="{flow_id}#0"]')
        expect(other.locator(".react-flow__edge-path")).to_have_css(
            "stroke", tokens.rgb("muted")
        )

        page.keyboard.press("Delete")
        _landed(page, 1)
        expect(page.locator(".react-flow__edge")).to_have_count(1)
        stored = catalog_api.state(draft_id)["snapshot"]["flows"][flow_id]
        assert stored["columns"] == [{"from_column": "id", "to_column": "id"}]

        # последняя пара снята клавишей: поток исчезает целиком
        _click_line(page, f"{flow_id}#0")
        page.keyboard.press("Delete")
        _landed(page, 2)
        expect(page.locator(".react-flow__edge")).to_have_count(0)
        assert catalog_api.state(draft_id)["snapshot"]["flows"] == {}

    def test_line_is_dragged_aside_without_selecting_it(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Линию можно оттащить за середину: её путь и ярлык уезжают за
        мышью, линия не выбирается, в черновик ничего не пишется."""
        _open_draft(page, stand, draft_id)
        flow_id = next(iter(catalog_api.state(draft_id)["snapshot"]["flows"]))
        edge = page.locator(f'.react-flow__edge[data-id="{flow_id}#0"]')
        grip = edge.locator('[data-testid="flow-edge-grip"]')
        label = page.locator(Selector.EDGE_LABEL)
        expect(grip).to_have_attribute("data-bent", "false")
        before = settled_box(page, label)
        path_before = edge.locator(".react-flow__edge-path").get_attribute("d")

        start = grip.bounding_box()
        assert start is not None
        page.mouse.move(*_centre(start))
        page.mouse.down()
        page.mouse.move(
            start["x"] + start["width"] / 2,
            start["y"] + start["height"] / 2 + 120,
            steps=10,
        )
        page.mouse.up()

        expect(grip).to_have_attribute("data-bent", "true")
        after = Css.box(label)
        assert after.y > before["y"] + 80, (before, after)
        assert edge.locator(".react-flow__edge-path").get_attribute("d") != path_before
        expect(edge).not_to_have_class(re.compile(r"\bselected\b"))
        assert catalog_api.state(draft_id)["seq"] == 0

        # клик без сдвига по-прежнему выбирает линию: ярлык стоит на ней
        anchor = label.bounding_box()
        assert anchor is not None
        page.mouse.click(*_centre(anchor))
        expect(edge).to_have_class(re.compile(r"\bselected\b"))


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
        pane = _open_source_tree(page, catalog_seed)
        source = pane.locator(catalog_seed.tree_object(Ed.EVENTS)).locator(
            ".tree__label"
        )
        _drag(page, source, _frame(page, Ed.DST), (20, 40))
        expect(page.locator(catalog_seed.node(Ed.EVENTS))).to_be_visible(
            timeout=LIVE_TIMEOUT_MS
        )
        expect(page.get_by_test_id("rebase-button")).to_have_count(0)

        version = catalog_api.publish_ops(
            catalog_seed.process_id,
            "edit other",
            [
                {
                    "op": "add_group",
                    "group": {"id": str(UUID(int=0xE0F1)), "name": "ed_other"},
                }
            ],
        )
        # действия черновика живут на вкладке process
        page.get_by_test_id("left-pane").get_by_role("tab", name="process").click()
        rebase = page.get_by_test_id("rebase-button")
        expect(rebase).to_have_text(f"update to v{version}", timeout=LIVE_TIMEOUT_MS)

        page.get_by_test_id("publish-button").click()
        conflict = _dialog(page, "publish-conflict")
        expect(conflict).to_be_visible()
        expect(conflict).to_contain_text(f"published catalog is at v{version}")
        conflict.get_by_role("button", name="update the draft").click()
        expect(conflict).to_have_count(0)
        expect(rebase).to_have_count(0)
        expect(_frame(page, "ed_other")).to_be_visible()
        expect(page.locator(catalog_seed.node(Ed.EVENTS))).to_be_visible()

        page.get_by_test_id("publish-button").click()
        page.wait_for_url(
            re.compile(rf"/catalog/processes/{catalog_seed.process_id}$"),
            timeout=30_000,
        )
        expect(page.get_by_test_id("version-chip")).to_have_text(f"v{version + 1}")

        assert catalog_seed.address(Ed.EVENTS) in catalog_api.node_addresses(
            catalog_seed.process_id
        )
        assert catalog_api.state(draft_id)["draft"]["status"] == "published"


class TestStaleness:
    def test_new_snapshot_version_marks_nodes_stale_and_pins_are_raised(
        self,
        page: Page,
        stand: StandProcess,
        catalog_api: Api,
        catalog_seed: Seed,
        draft_id: str,
    ) -> None:
        """Подключение получает версию снимка без ed_returns: узел помечен
        устаревшим по событию, панель называет причину, «raise pins» поднимает
        привязки и перечисляет, что перестало сходиться; перенацеливание узла
        на живой объект снимает устаревание."""
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
            catalog_seed.connection_id: 2
        }

        page.get_by_test_id("detail-panel").get_by_role(
            "button", name="retarget node"
        ).click()
        objects = _pick_object(page, catalog_seed, Ed.ARCHIVE)
        objects.get_by_role("button", name=f"retarget {Ed.RETURNS} here").click()
        expect(page.locator(catalog_seed.node(Ed.ARCHIVE))).to_be_visible()
        expect(catalog).to_have_attribute("data-stale", "0")
