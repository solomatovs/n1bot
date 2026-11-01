"""Пользователь добавляет, правит, выключает и удаляет MCP-сервер в чате."""

import pytest
from chat_ui import OpenChat
from playwright.sync_api import expect
from test_mcp_app_ui import McpAppPage
from test_mcp_app_ui import live_address as live_address
from test_mcp_app_ui import live_host as live_host
from test_mcp_app_ui import mcp_stand as mcp_stand

from boba.aid.chat.ui_text import UiText
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("mcp-app-ui")]


class TestMcpForm:
    def test_role_connection_has_disabled_settings(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        app_texts: UiText,
        live_address: str,
    ) -> None:
        view = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        view.run()
        view.server("live").click()
        card = view.card("live")
        expect(card.locator("[data-mcp-edit]")).to_have_count(0)
        expect(card.locator("[data-mcp-toggle]")).to_have_count(0)
        card.locator("[data-mcp-settings]").click()
        form = view.page.locator("[data-mcp-readonly]").last
        expect(form.locator('[name="mcp-name"]')).to_have_value("live")
        expect(form.locator('[name="mcp-url"]')).to_have_value(live_address)
        for name in ("mcp-name", "mcp-url"):
            expect(form.locator(f'[name="{name}"]')).to_be_disabled()
        for kind in ("transport", "auth"):
            for button in form.locator(f'[data-mcp-choice="{kind}"] button').all():
                expect(button).to_be_disabled()

        expect(form.locator("[data-mcp-role-managed]")).to_contain_text(
            view.label("roleManaged")
        )
        expect(form.locator('button[type="submit"]')).to_have_count(0)
        expect(form.locator("[data-mcp-remove]")).to_have_count(0)
        form.get_by_role("button", name=view.label("back"), exact=True).click()
        expect(view.card("live")).to_contain_text(live_address)

    def test_user_manages_a_server(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        app_texts: UiText,
        live_address: str,
    ) -> None:
        view = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        view.run()
        view.app().locator("[data-mcp-add]").click()
        form = view.page.locator("[data-mcp-form]").last
        form.locator('[name="mcp-name"]').fill("user-live")
        form.locator('[name="mcp-url"]').fill("ftp://invalid.example/mcp")
        form.get_by_role("button", name=view.label("create"), exact=True).click()
        expect(form.locator("[data-mcp-issue]")).to_contain_text("http or https")
        form.locator('[name="mcp-url"]').fill(live_address)
        form.get_by_role("button", name=view.label("create"), exact=True).click()
        card = view.card("user-live")
        expect(card).to_contain_text(view.label("state.connected"), timeout=60_000)
        expect(card).to_contain_text(view.label("yours"))
        card.locator("[data-mcp-edit]").click()
        form = view.page.locator("[data-mcp-form]").last
        expect(form.locator('[name="mcp-url"]')).to_have_value(live_address)
        form.locator('[name="mcp-name"]').fill("user-renamed")
        form.locator('[name="mcp-url"]').fill(live_address + "/missing")
        form.get_by_role("button", name=view.label("save"), exact=True).click()
        card = view.card("user-renamed")
        expect(card).to_contain_text(view.label("state.failed"), timeout=60_000)
        card.locator("[data-mcp-toggle]").click()
        expect(card).to_contain_text(view.label("state.disabled"))
        card.locator("[data-mcp-toggle]").click()
        expect(card).to_contain_text(view.label("state.failed"), timeout=60_000)
        card.get_by_role("button", name=view.label("back"), exact=True).click()
        view.app().locator('[data-mcp-row-remove="user-renamed"]').click()
        form = view.page.locator("[data-mcp-form]").last
        confirm = form.locator("[data-mcp-remove-confirm]")
        remove = confirm.get_by_role("button", name=view.label("remove"), exact=True)
        expect(remove).to_be_disabled()
        confirm.get_by_role("textbox").fill("user-renamed")
        remove.click()
        expect(view.app().locator('[data-mcp-server="user-renamed"]')).to_have_count(0)
        expect(view.app().locator('[data-mcp-server="user-live"]')).to_have_count(0)
