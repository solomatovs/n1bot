"""Пользователь добавляет, правит, выключает и удаляет MCP-сервер в чате."""

from pathlib import Path

import pytest
from chat_ui import OpenChat
from playwright.sync_api import expect
from test_mcp_app_ui import McpAppPage
from test_mcp_app_ui import live_address as live_address
from test_mcp_app_ui import live_host as live_host
from test_mcp_app_ui import mcp_stand as mcp_stand

from boba.aid.chat.ui_text import UiText
from boba.aid.mcp_servers.oauth import McpClientMetadataDocument
from boba.stand.service_signin import FakeServiceProcess
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("mcp-app-ui")]


class TestMcpForm:
    def test_user_can_select_browser_oauth(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        app_texts: UiText,
    ) -> None:
        view = McpAppPage(open_chat(mcp_stand, ""), app_texts)
        view.run()
        view.app().locator("[data-mcp-add]").click()
        form = view.page.locator("[data-mcp-form]").last
        form.locator('[name="mcp-name"]').fill("user-oauth")
        form.locator('[name="mcp-url"]').fill("http://127.0.0.1:9/mcp")
        form.locator('[data-mcp-choice="auth"] button[value="oauth"]').click()
        expect(form.locator('[name="mcp-token"]')).to_have_count(0)
        expect(
            form.locator("[data-mcp-oauth-form] [data-mcp-oauth-connect]")
        ).to_be_visible()
        form.get_by_role("button", name=view.label("create"), exact=True).click()
        card = view.card("user-oauth")
        expect(card.locator("[data-mcp-oauth-connect]")).to_be_visible()
        card.locator("[data-mcp-edit]").click()
        form = view.page.locator("[data-mcp-form]").last
        selected = form.locator('[data-mcp-choice="auth"] button[value="oauth"]')
        expect(selected).to_have_attribute("aria-checked", "true")
        form.locator("[data-mcp-form-close]").click()
        expect(view.server("user-oauth")).to_have_attribute("aria-expanded", "true")
        view.app().locator('[data-mcp-row-remove="user-oauth"]').click()
        confirm = view.page.locator("[data-mcp-remove-confirm]").last
        confirm.get_by_role("textbox").fill("user-oauth")
        confirm.get_by_role("button", name=view.label("remove"), exact=True).click()

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
        expect(card.locator("[data-mcp-row-toggle]")).to_be_disabled()
        expect(card.locator("[data-mcp-row-toggle]")).to_be_checked()
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
        form.locator("[data-mcp-form-close]").click()
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
        expect(view.server("user-live")).to_have_attribute("aria-expanded", "true")
        expect(view.server("live")).to_be_visible()
        card.locator("[data-mcp-edit]").click()
        form = view.page.locator("[data-mcp-form]").last
        expect(form.locator('[name="mcp-url"]')).to_have_value(live_address)
        form.locator('[name="mcp-name"]').fill("user-renamed")
        form.locator('[name="mcp-url"]').fill(live_address + "/missing")
        form.get_by_role("button", name=view.label("save"), exact=True).click()
        card = view.card("user-renamed")
        expect(card).to_contain_text(view.label("state.failed"), timeout=60_000)
        checkbox = card.get_by_role("checkbox")
        expect(checkbox).to_be_checked()
        checkbox.click()
        expect(card).to_contain_text(view.label("state.disabled"))
        expect(checkbox).not_to_be_checked()
        expect(card.locator("[data-mcp-row-reconnect]")).to_be_disabled()
        expect(view.server("user-renamed")).to_have_attribute("aria-expanded", "true")
        view.server("user-renamed").click()
        expect(card.locator("[data-mcp-details]")).to_be_hidden()
        checkbox.click()
        expect(card).to_contain_text(view.label("state.failed"), timeout=60_000)
        expect(checkbox).to_be_checked()
        expect(card.locator("[data-mcp-row-reconnect]")).to_be_enabled()
        expect(view.server("user-renamed")).to_have_attribute("aria-expanded", "false")
        view.app().locator('[data-mcp-row-remove="user-renamed"]').click()
        form = view.page.locator("[data-mcp-form]").last
        confirm = form.locator("[data-mcp-remove-confirm]")
        remove = confirm.get_by_role("button", name=view.label("remove"), exact=True)
        expect(remove).to_be_disabled()
        confirm.get_by_role("textbox").fill("user-renamed")
        remove.click()
        expect(view.app().locator('[data-mcp-server="user-renamed"]')).to_have_count(0)
        expect(view.app().locator('[data-mcp-server="user-live"]')).to_have_count(0)

    def test_oauth_form_saves_connects_and_returns_to_the_chat(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        app_texts: UiText,
        tmp_path: Path,
    ) -> None:
        remote = FakeServiceProcess(tmp_path, 300)
        remote.wait()
        try:
            view = McpAppPage(open_chat(mcp_stand, ""), app_texts)
            self._client_document(view, mcp_stand.config.base_url)
            view.run()
            return_url = self._return_page(view)
            view.app().locator("[data-mcp-add]").click()
            form = view.page.locator("[data-mcp-form]").last
            form.locator('[name="mcp-name"]').fill("oauth-roundtrip")
            form.locator('[name="mcp-url"]').fill(remote.url() + "/")
            choice = form.locator('[data-mcp-choice="auth"] button[value="oauth"]')
            choice.click()
            expect(choice).to_have_attribute("aria-checked", "true")
            form.locator("[data-mcp-oauth-connect]").click()
            self._oauth_return(view, remote, return_url)
            view.server("oauth-roundtrip").click()
            card = view.card("oauth-roundtrip")
            expect(card).to_contain_text(view.label("state.connected"), timeout=60_000)
            card.get_by_text(view.label("showTools")).click()
            tool = card.locator('[data-mcp-tool="fake_echo"]')
            expect(tool.locator("[data-mcp-tool-name]")).to_have_text("fake_echo")
            expect(tool.locator("[data-mcp-tool-description]")).to_be_visible()
            expect(tool.locator("[data-mcp-tool-json]")).to_be_hidden()
            tool.locator("summary").click()
            expect(tool.locator("[data-mcp-tool-json]")).to_be_visible()
            expect(tool.locator("[data-mcp-tool-json]")).to_contain_text("Что вернуть")
            card.screenshot(path=str(tmp_path / "mcp-tool-card.png"))

            card.locator("[data-mcp-oauth-disconnect]").click()
            expect(card).to_contain_text(view.label("state.needs_auth"))
            expect(card.locator("[data-mcp-issue]")).to_contain_text(
                view.label("issueAuth")
            )
            expect(card).not_to_contain_text("handshake failed")
            expect(card.locator("[data-mcp-tool]")).to_have_count(0)
            card.locator("[data-mcp-row-reconnect]").click()
            expect(card).to_contain_text(view.label("state.needs_auth"))
            assert "OAuth flow error" not in mcp_stand.log_path.read_text()

            card.locator("[data-mcp-oauth-connect]").click()
            self._oauth_return(view, remote, return_url)
            view.server("oauth-roundtrip").click()
            expect(view.card("oauth-roundtrip")).to_contain_text(
                view.label("state.connected"), timeout=60_000
            )
        finally:
            remote.stop()

    def test_oauth_missing_endpoint_shows_a_short_connection_error(
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
        address = live_address + "/missing"
        form.locator('[name="mcp-name"]').fill("oauth-missing")
        form.locator('[name="mcp-url"]').fill(address)
        form.locator('[data-mcp-choice="auth"] button[value="oauth"]').click()
        form.locator("[data-mcp-oauth-connect]").click()
        issue = form.locator("[data-mcp-issue]")
        expect(issue).to_contain_text("HTTP 404 Not Found", timeout=60_000)
        expect(issue).to_contain_text(f"Could not connect to MCP server {address}")
        expect(issue).not_to_contain_text("TypeError")
        expect(issue).not_to_contain_text("connecting and initializing")
        assert issue.inner_text().count(address) == 1
        assert "MCP action failed:" in mcp_stand.log_path.read_text()

    @staticmethod
    def _client_document(view: McpAppPage, app_url: str) -> None:
        base = app_url.rstrip("/")
        response = view.page.request.get(base + "/api/mcp/oauth/client")
        assert response.ok
        metadata = McpClientMetadataDocument.model_validate(response.json())
        assert str(metadata.client_id) == base + "/api/mcp/oauth/client"
        assert metadata.redirect_uris is not None
        assert [str(uri) for uri in metadata.redirect_uris] == [
            base + "/api/mcp/oauth/callback"
        ]

    @staticmethod
    def _return_page(view: McpAppPage) -> str:
        view.app()
        thread_id = view.chat.log.thread_id()
        source = f"{view.chat.base_url}/thread/{thread_id}?mcp=tools#tool-card"
        view.page.goto(source, wait_until="domcontentloaded")
        view.app()
        expect(view.page).to_have_url(source)
        return source

    @staticmethod
    def _oauth_return(
        view: McpAppPage, remote: FakeServiceProcess, return_url: str
    ) -> None:
        view.page.locator('[name="login"]').fill(remote.LOGIN, timeout=60_000)
        view.page.locator('[name="password"]').fill(remote.PASSWORD)
        view.page.get_by_role("button", name="Sign in", exact=True).click()
        expect(view.page).to_have_url(return_url, timeout=60_000)
