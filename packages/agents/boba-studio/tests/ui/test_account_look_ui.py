"""Внешний вид входа, кабинета и формы по схеме: DOM, стили по токенам, виджеты."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect
from studio_ui import login_cookies

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.look import Css, Tokens, no_horizontal_scroll
from boba.stand.ui.stand import StandApp, StandProcess

pytestmark = pytest.mark.ui

WIDE: ViewportSize = {"width": 1280, "height": 900}
NARROW: ViewportSize = {"width": 640, "height": 800}


class Sel:
    """Селекторы входа и кабинета: одно место на разметку."""

    LOGIN_CARD: ClassVar[str] = ".login__card"
    LOGIN_FORM: ClassVar[str] = 'form[aria-label="sign in"]'
    LOGIN_SUBMIT: ClassVar[str] = ".login__submit"
    LOGIN_SSO: ClassVar[str] = ".login__sso"
    ALERT_ERROR: ClassVar[str] = ".alert--error"
    ALERT_OK: ClassVar[str] = ".alert--ok"
    ALERT_INFO: ClassVar[str] = ".alert--info"
    ALERT_ICON: ClassVar[str] = ".alert__icon"
    ACCOUNT_LOGIN: ClassVar[str] = ".account__login"
    ACCOUNT_META: ClassVar[str] = ".account__meta"
    CRUMBS: ClassVar[str] = ".crumbs"
    SIGN_OUT: ClassVar[str] = 'button[aria-label="Sign out"]'
    BACK: ClassVar[str] = 'a[aria-label="Back to studio"]'
    TAB: ClassVar[str] = '[role="tab"]'
    CONNECTIONS: ClassVar[str] = ".connections"
    CONNECTIONS_LIST: ClassVar[str] = ".connections__list"
    CONNECTIONS_SCENE: ClassVar[str] = ".connections__scene"
    LIST_GROUP: ClassVar[str] = ".list__group"
    LIST_NEW: ClassVar[str] = ".list__new"
    ITEM: ClassVar[str] = ".connections__list .item"
    FORM: ClassVar[str] = 'form[aria-label="connection"]'
    KIND: ClassVar[str] = 'select[aria-label="profile.kind"]'
    AUTH_BLOCK: ClassVar[str] = 'fieldset[data-path="profile.auth"]'
    AUTH_METHOD: ClassVar[str] = 'select[aria-label="profile.auth.method"]'
    POOL_TOGGLE: ClassVar[str] = (
        'fieldset[data-path="profile.pool"] .schema-block__toggle'
    )
    FIELD_INVALID: ClassVar[str] = ".field--invalid"
    FIELD_ISSUE: ClassVar[str] = ".field__issue"
    FIELD_HINT: ClassVar[str] = ".field__hint"
    REQUIRED: ClassVar[str] = ".field__required"
    LAMP: ClassVar[str] = ".topbar .lamp"
    PROFILE_CHIP: ClassVar[str] = ".profile-chip"
    PROFILE_SELECT: ClassVar[str] = 'select[aria-label="profile"]'
    GEAR: ClassVar[str] = 'a[aria-label="Account"]'


@pytest.fixture(scope="module")
def tokens() -> Tokens:
    return Tokens.load()


@pytest.fixture(scope="module")
def shared_connections(stand: StandProcess, llm_port: int) -> None:
    """Общие соединения стенда (main pg/ch, stand web) выданы ролям после старта."""
    StandDatabase(StandApp.STUDIO, stand.config.db_name).seed_connections(llm_port)


def _context_page(
    browser: Browser, stand: StandProcess, viewport: ViewportSize, signed: bool
) -> Iterator[Page]:
    context = browser.new_context(viewport=viewport)
    if signed:
        context.add_cookies(login_cookies(stand))
    opened = context.new_page()
    opened.set_default_timeout(30_000)
    try:
        yield opened
    finally:
        context.close()


@pytest.fixture
def anonymous(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    yield from _context_page(browser, stand, WIDE, signed=False)


@pytest.fixture
def page(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    yield from _context_page(browser, stand, WIDE, signed=True)


@pytest.fixture
def narrow_page(browser: Browser, stand: StandProcess) -> Iterator[Page]:
    yield from _context_page(browser, stand, NARROW, signed=True)


def _open(page: Page, stand: StandProcess, path: str) -> None:
    page.goto(f"{stand.config.base_url}/workflow{path}", wait_until="domcontentloaded")


def _open_new_connection(page: Page, stand: StandProcess) -> None:
    """Форма нового соединения на виде postgres: порядок видов задаёт реестр
    плагинов, поэтому вид выбирается явно."""
    _open(page, stand, "/account")
    page.locator(Sel.LIST_NEW).click()
    expect(page.locator(Sel.FORM)).to_be_visible()
    page.locator(Sel.KIND).select_option("postgres")


class TestLogin:
    def test_card_is_centered_and_styled(
        self, anonymous: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(anonymous, stand, "/login")
        card = anonymous.locator(Sel.LOGIN_CARD)
        expect(card).to_be_visible()

        box = Css.box(card)
        assert box.width <= 380
        assert abs((box.x + box.width / 2) - WIDE["width"] / 2) < 2
        assert Css.of(card, "background-color") == tokens.rgb("surface")
        assert Css.of(card, "border-top-color") == tokens.rgb("hairline")
        assert no_horizontal_scroll(anonymous)

        form = anonymous.locator(Sel.LOGIN_FORM)
        expect(form.locator('input[name="username"]')).to_be_visible()
        expect(form.locator('input[name="password"]')).to_have_attribute(
            "type", "password"
        )
        login_input = form.locator('input[name="username"]')
        assert Css.of(login_input, "background-color") == tokens.rgb("bg")
        assert "mono" in Css.of(login_input, "font-family").lower()

        submit = anonymous.locator(Sel.LOGIN_SUBMIT)
        expect(submit).to_have_text("Sign in")
        assert Css.of(submit, "background-color") == tokens.rgb("signal")
        assert Css.of(submit, "color") == tokens.rgb("on-signal")
        # локальный стенд без kerberos: кнопки SSO нет
        expect(anonymous.locator(Sel.LOGIN_SSO)).to_have_count(0)

    def test_wrong_password_shows_an_error_alert(
        self, anonymous: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(anonymous, stand, "/login")
        form = anonymous.locator(Sel.LOGIN_FORM)
        form.locator('input[name="username"]').fill("admin")
        form.locator('input[name="password"]').fill("wrong")
        anonymous.locator(Sel.LOGIN_SUBMIT).click()

        alert = anonymous.locator(Sel.ALERT_ERROR)
        expect(alert).to_be_visible()
        expect(alert).to_have_attribute("role", "alert")
        expect(alert).to_contain_text("Invalid username or password")
        assert Css.of(alert, "border-left-color") == tokens.rgb("error")
        assert Css.of(alert.locator(Sel.ALERT_ICON), "color") == tokens.rgb("error")
        assert Css.of(alert, "display") == "flex"
        assert no_horizontal_scroll(anonymous)


@pytest.mark.usefixtures("shared_connections")
class TestAccount:
    def test_header_facts_and_tabs(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/account")
        login = stand.config.credential().login
        expect(page.locator(Sel.ACCOUNT_LOGIN)).to_have_text(login)
        assert (
            "grotesk" in Css.of(page.locator(Sel.ACCOUNT_LOGIN), "font-family").lower()
        )

        meta = page.locator(Sel.ACCOUNT_META)
        expect(meta).to_contain_text("roles: ADM")
        expect(meta).to_contain_text("sign-in: LocalAuth")
        assert Css.of(meta, "color") == tokens.rgb("muted")

        expect(page.locator(Sel.CRUMBS)).to_have_text("Account")
        expect(page.locator(Sel.SIGN_OUT)).to_be_visible()
        expect(page.locator(Sel.BACK)).to_have_attribute(
            "href", f"{stand.config.url_prefix}/workflow/workflow"
        )

        tab = page.locator(Sel.TAB, has_text="Connections")
        expect(tab).to_have_attribute("aria-selected", "true")
        assert Css.of(tab, "border-radius") == tokens.raw("r-pill")

    def test_connections_layout_wide_and_narrow(
        self, page: Page, narrow_page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open(page, stand, "/account")
        wide = page.locator(Sel.CONNECTIONS)
        expect(wide).to_be_visible()
        assert Css.of(wide, "display") == "grid"
        columns = Css.of(wide, "grid-template-columns").split()
        assert len(columns) == 2
        assert Css.of(
            page.locator(Sel.CONNECTIONS_LIST), "background-color"
        ) == tokens.rgb("surface")
        groups = page.locator(Sel.LIST_GROUP)
        expect(groups).to_have_count(2)
        expect(groups.nth(0)).to_contain_text("mine")
        expect(groups.nth(1)).to_contain_text("shared")
        # общие соединения стенда посеяны для ролей: main (pg, ch) и stand (web)
        expect(page.locator(Sel.ITEM)).to_have_count(3)
        expect(page.locator(Sel.CONNECTIONS_SCENE)).to_contain_text("Pick a connection")

        _open(narrow_page, stand, "/account")
        narrow = narrow_page.locator(Sel.CONNECTIONS)
        expect(narrow).to_be_visible()
        assert len(Css.of(narrow, "grid-template-columns").split()) == 1
        assert no_horizontal_scroll(narrow_page)

    def test_shared_connection_is_read_only(
        self, page: Page, stand: StandProcess
    ) -> None:
        _open(page, stand, "/account")
        page.locator(Sel.ITEM, has_text="stand").click()

        expect(page.locator(Sel.ALERT_INFO)).to_contain_text("read-only")
        expect(page.locator(Sel.KIND)).to_have_value("web")
        expect(page.locator(Sel.KIND)).to_be_disabled()
        expect(page.get_by_label("profile.base_url", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Save", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Delete", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Check", exact=True)).to_be_visible()


@pytest.mark.usefixtures("shared_connections")
class TestSchemaForm:
    def test_widgets_follow_the_schema(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open_new_connection(page, stand)

        # обязательные поля помечены; вложенный auth — блок с пикером варианта
        name = page.get_by_label("connection name")
        expect(name.locator("xpath=..").locator(Sel.REQUIRED)).to_have_count(1)
        auth = page.locator(Sel.AUTH_BLOCK)
        expect(auth).to_be_visible()
        expect(auth.locator("legend")).to_contain_text("auth")
        assert Css.of(auth, "border-top-color") == tokens.rgb("hairline")
        method = page.locator(Sel.AUTH_METHOD)
        expect(method).to_be_visible()

        # секрет — password-поле, число — number, подсказка — из описания модели
        method.select_option("password")
        expect(
            page.get_by_label("profile.auth.password", exact=True)
        ).to_have_attribute("type", "password")
        expect(page.get_by_label("profile.port", exact=True)).to_have_attribute(
            "type", "number"
        )
        hint = (
            page.get_by_label("profile.host", exact=True)
            .locator("xpath=..")
            .locator(Sel.FIELD_HINT)
        )
        expect(hint).to_contain_text("Хост")
        assert Css.of(hint, "color") == tokens.rgb("faint")

        # вложенный объект по умолчанию свёрнут, раскрывается кнопкой
        toggle = page.locator(Sel.POOL_TOGGLE)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_label("profile.pool.min_size", exact=True)).to_have_count(0)
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(page.get_by_label("profile.pool.min_size", exact=True)).to_have_value(
            "1"
        )

        # смена kind перестраивает поддерево: у web чекбокс TLS в строку
        page.locator(Sel.KIND).select_option("web")
        expect(page.locator(Sel.AUTH_METHOD)).to_have_value("none")
        ssl = page.get_by_label("profile.ssl_verify", exact=True)
        expect(ssl).to_have_attribute("type", "checkbox")
        expect(ssl).to_be_checked()
        assert Css.of(ssl.locator("xpath=.."), "flex-direction") == "row"

    def test_server_issues_land_under_the_field(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open_new_connection(page, stand)
        page.get_by_label("connection name").fill("look-invalid")
        page.get_by_label("profile.host", exact=True).fill("db.test")
        page.get_by_label("profile.dbname", exact=True).fill("boba")
        page.locator(Sel.AUTH_METHOD).select_option("trust")
        page.get_by_role("button", name="Save", exact=True).click()

        # user пуст: 422 от сервера подсвечивает именно это поле
        invalid = page.locator(Sel.FIELD_INVALID)
        expect(invalid).to_have_count(1)
        expect(invalid).to_have_attribute("data-path", "profile.auth.user")
        assert Css.of(invalid.locator(".input"), "border-top-color") == tokens.rgb(
            "error"
        )
        issue = invalid.locator(Sel.FIELD_ISSUE)
        expect(issue).to_contain_text("at least 1 character")
        assert Css.of(issue, "color") == tokens.rgb("error")
        expect(page.locator(Sel.ALERT_ERROR)).to_contain_text("Check 1 field(s)")

    def test_check_shows_ok_alert(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        _open_new_connection(page, stand)
        page.locator(Sel.KIND).select_option("web")
        page.get_by_label("profile.base_url", exact=True).fill(
            f"http://127.0.0.1:{stand.config.llm_port}/health"
        )
        page.get_by_role("button", name="Check", exact=True).click()

        ok = page.locator(Sel.ALERT_OK)
        expect(ok).to_contain_text("Connected")
        expect(ok).to_contain_text("HTTP 200")
        expect(ok).to_contain_text("ms")
        expect(ok).to_have_attribute("role", "status")
        assert Css.of(ok, "border-left-color") == tokens.rgb("signal")


class TestTopbarWidgets:
    def test_lamp_without_profile_chip(
        self, page: Page, stand: StandProcess, tokens: Tokens
    ) -> None:
        """Лампочка живёт; чипа профиля в studio больше нет — профиль здесь
        всегда общий, его роль займут сами workflow."""
        _open(page, stand, "/observe")
        lamp = page.locator(Sel.LAMP)
        expect(lamp).to_have_attribute("data-socket", "connected")
        expect(lamp).to_have_attribute("data-bus", "listening")
        expect(lamp).to_have_attribute("role", "status")
        assert Css.of(lamp, "background-color") == tokens.rgb("status-done")
        assert Css.of(lamp, "border-radius") == "50%"
        assert Css.of(lamp, "box-shadow") != "none"

        expect(page.locator(Sel.PROFILE_CHIP)).to_have_count(0)
        expect(page.locator(Sel.PROFILE_SELECT)).to_have_count(0)

        gear = page.locator(Sel.GEAR)
        expect(gear).to_have_attribute(
            "href", f"{stand.config.url_prefix}/workflow/account"
        )
