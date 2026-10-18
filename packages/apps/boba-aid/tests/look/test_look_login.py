"""Вид страницы входа: форма с кнопкой SSO, только SSO, только пароль и каждая
ошибка входа — неверный пароль, нет ролей, отказы SSO — в обеих темах и обоих
размерах окна. Ошибки снимаются на обоих языках интерфейса: их текст, и в
баннере формы, и в уведомлении, берётся из переводов по коду отказа.
Клавиатурный фокус поля и кнопки снимается на широком окне в обеих темах.

Страница целиком — кадр: на ней ничего, кроме формы и уведомления, и
раскладка формы меняется с шириной окна.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from aid_look import (
    LookAxes,
    LookCheck,
    LookError,
    LookFixes,
    LookLanguage,
    LookPages,
    LookPaths,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Locator, Page, expect

from boba.aid.auth.composite import SignInCode
from boba.aid.chat.ui_text import UiText
from boba.identity.sso import SsoErrorCode
from boba.stand.ui.stand import StandAuth, StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]


class LoginLook:
    """Состояния страницы входа (LG плана тестов вида) и её селекторы."""

    GROUP: ClassVar[str] = "login"
    WITH_SSO: ClassVar[LookState] = LookState(
        GROUP,
        "lg1-password-and-sso",
        "Вход по паролю и через SSO: поля логина и пароля, «Войти», «Войти через SSO»",
        "одна колонка без картинки, логотип N1 по центру (в тёмной теме белый); "
        "подсказки полей «Введите логин» и «Введите пароль»; обе кнопки высотой "
        "40 px с углами 4 px, в светлой теме синий Consta; на узком окне форма "
        "во всю ширину без прокрутки вбок",
        (LookFixes.N3,),
    )
    SSO_ONLY: ClassVar[LookState] = LookState(
        GROUP,
        "lg2-sso-only",
        "Вход только через SSO: кнопка без полей",
        "та же одна колонка, что и с паролем: справа нет картинки, логотип по "
        "центру, кнопка «Войти через SSO» того же размера, что «Войти» в lg1",
        (LookFixes.N33,),
    )
    PASSWORD_ONLY: ClassVar[LookState] = LookState(
        GROUP,
        "lg3-password-only",
        "Вход только по паролю: без кнопки SSO",
        "как lg1, но без «Войти через SSO» и разделителя; подсказки полей по-русски",
        (LookFixes.N3,),
    )
    FOCUS_FIELD: ClassVar[LookState] = LookState(
        GROUP,
        "lg1-focus-field",
        "Tab с клавиатуры встал на поле логина",
        "рамка поля логина цвета кольца темы (в светлой — синий Consta), "
        "внешнего кольца у поля нет, остальные контролы без выделения",
        (LookFixes.FOCUS,),
    )
    FOCUS_BUTTON: ClassVar[LookState] = LookState(
        GROUP,
        "lg1-focus-button",
        "Tab с клавиатуры встал на кнопку «Войти»",
        "вокруг «Войти» кольцо 2 px с зазором 2 px: в светлой теме синий Consta "
        "на белом, в тёмной — голубое на тёмном, видно в обеих; поля без выделения",
        (LookFixes.FOCUS,),
    )

    SSO_BUTTON: ClassVar[str] = "#sso-login-btn"
    LOGIN: ClassVar[str] = "#email"
    PASSWORD: ClassVar[str] = "#password"
    SUBMIT: ClassVar[str] = 'form button[type="submit"]'
    TOAST: ClassVar[str] = "[data-sonner-toast]"
    WRONG: ClassVar[str] = "not-the-password"
    NO_ROLES: ClassVar[str] = "norole"
    ERRORS: ClassVar[str] = "auth.login.errors"
    RING: ClassVar[str] = "solid 2px"
    NO_RING: ClassVar[str] = "none"

    def error_key(self, code: str) -> str:
        return f"{self.ERRORS}.{code}"


class LoginErrors:
    """Состояния ошибок входа на языке language: имя состояния несёт язык."""

    def __init__(self, language: LookLanguage) -> None:
        self._suffix = language.value.split("-")[0]

    def wrong_password(self) -> LookState:
        return self._state(
            "lg4-wrong-password",
            "Неверный пароль: баннер над формой и уведомление, текст по коду",
            "баннер над формой и уведомление внизу говорят одно и то же на языке "
            "кадра, без «Unauthorized» и кода отказа; поля сохранили введённое",
        )

    def access_denied(self) -> LookState:
        return self._state(
            "lg4-access-denied",
            "Учётной записи не назначены роли: баннер и уведомление «доступ запрещён»",
            "баннер и уведомление — «доступ запрещён» на языке кадра, а не "
            "«неверный пароль»",
        )

    def sso(self, code: SsoErrorCode) -> LookState:
        captions = {
            SsoErrorCode.TICKET: (
                "lg5-sso-ticket",
                "Отказ SSO: браузер не предъявил Kerberos-билет",
                "баннер объясняет, что браузер не предъявил билет Kerberos; "
                "форма и кнопка SSO на месте",
            ),
            SsoErrorCode.DENIED: (
                "lg6-sso-denied",
                "Отказ SSO: учётной записи не даны роли",
                "баннер говорит, что учётной записи не назначены роли",
            ),
            SsoErrorCode.FAILED: (
                "lg7-sso-failed",
                "Ошибка входа через SSO",
                "баннер общей ошибки SSO с советом обратиться в поддержку",
            ),
        }
        name, caption, check = captions[code]
        return self._state(name, caption, check)

    def _state(self, name: str, caption: str, check: str) -> LookState:
        return LookState(LoginLook.GROUP, f"{name}-{self._suffix}", caption, check, ())


class LoginPage:
    """Страница входа стенда глазами теста вида."""

    def __init__(self, page: Page, stand: StandProcess) -> None:
        self._page = page
        self._stand = stand
        self._look = LoginLook()

    def open(self, query: str = "") -> None:
        url = f"{self._stand.config.base_url}/login{query}"
        self._page.goto(url, wait_until="domcontentloaded")

    def sso_button(self) -> None:
        expect(self._page.locator(LoginLook.SSO_BUTTON)).to_be_visible()

    def password_form(self) -> None:
        expect(self._page.locator(LoginLook.PASSWORD)).to_be_visible()

    def sign_in(self, login: str, password: str, error: str) -> None:
        """Вход, который сервер отказал: тот же текст и в баннере, и в уведомлении."""
        self._page.locator(LoginLook.LOGIN).fill(login)
        self._page.locator(LoginLook.PASSWORD).fill(password)
        self._page.locator(LoginLook.SUBMIT).click()
        toast = self._page.locator(LoginLook.TOAST)
        expect(toast).to_have_text(error)
        expect(self._page.locator("form").get_by_text(error)).to_be_visible()

    def banner(self, error: str) -> None:
        expect(self._page.get_by_text(error)).to_be_visible()

    def submit(self) -> Locator:
        return self._page.locator(LoginLook.SUBMIT)


def test_login_with_password_and_sso(
    look: LookCheck, look_pages: LookPages, look_stands: LookStand
) -> None:
    stand = look_stands.of(StandAuth.LOCAL_SSO)
    for theme, size in LookAxes().every():
        page = look_pages.open(theme, size)
        login = LoginPage(page, stand)

        login.open()
        login.sso_button()
        login.password_form()
        look_pages.settle(page)
        look.frame(page.locator("body"), LoginLook.WITH_SSO, theme, size)

    look.verify()


@pytest.mark.parametrize("language", list(LookLanguage))
def test_login_errors(
    look: LookCheck,
    look_pages: LookPages,
    look_stands: LookStand,
    language: LookLanguage,
) -> None:
    stand = look_stands.of(StandAuth.LOCAL_SSO, language)
    texts = UiText(LookPaths.ASSETS, language.value)
    keys = LoginLook()
    states = LoginErrors(language)
    admin = stand.config.credential()
    norole = stand.config.credential(LoginLook.NO_ROLES)
    for theme, size in LookAxes().every():
        page = look_pages.open(theme, size)
        login = LoginPage(page, stand)
        body = page.locator("body")

        login.open()
        login.password_form()
        wrong = texts.text(keys.error_key(SignInCode.CREDENTIALS.value))
        login.sign_in(admin.login, LoginLook.WRONG, wrong)
        look_pages.settle(page)
        look.frame(body, states.wrong_password(), theme, size)

        login.open()
        login.password_form()
        denied = texts.text(keys.error_key(SignInCode.DENIED.value))
        login.sign_in(norole.login, norole.password, denied)
        look_pages.settle(page)
        look.frame(body, states.access_denied(), theme, size)

        for code in SsoErrorCode:
            login.open(f"?error={code.value}")
            login.banner(texts.text(keys.error_key(code.value)))
            login.sso_button()
            look_pages.settle(page)
            look.frame(body, states.sso(code), theme, size)

    look.verify()


def test_login_with_sso_only(
    look: LookCheck, look_pages: LookPages, look_stands: LookStand
) -> None:
    stand = look_stands.of(StandAuth.SSO)
    for theme, size in LookAxes().every():
        page = look_pages.open(theme, size)
        login = LoginPage(page, stand)

        login.open()
        login.sso_button()
        look_pages.settle(page)
        look.frame(page.locator("body"), LoginLook.SSO_ONLY, theme, size)

    look.verify()


def test_login_with_password_only(
    look: LookCheck, look_pages: LookPages, look_stands: LookStand
) -> None:
    stand = look_stands.of(StandAuth.LOCAL)
    for theme, size in LookAxes().every():
        page = look_pages.open(theme, size)
        login = LoginPage(page, stand)

        login.open()
        login.password_form()
        look_pages.settle(page)
        look.frame(page.locator("body"), LoginLook.PASSWORD_ONLY, theme, size)

    look.verify()


def test_login_keyboard_focus(
    look: LookCheck, look_pages: LookPages, look_stands: LookStand
) -> None:
    """Tab рисует кольцо фокуса темы, клик мышью — нет."""
    stand = look_stands.of(StandAuth.LOCAL_SSO)
    for theme in LookTheme:
        page = look_pages.open(theme, LookSize.WIDE)
        login = LoginPage(page, stand)
        body = page.locator("body")

        login.open()
        login.sso_button()
        login.password_form()
        look_pages.settle(page)
        look_pages.tab_to(page, LoginLook.LOGIN)
        look.frame(body, LoginLook.FOCUS_FIELD, theme, LookSize.WIDE)

        look_pages.tab_to(page, LoginLook.SUBMIT)
        keyboard = look_pages.outline(login.submit())
        look.frame(body, LoginLook.FOCUS_BUTTON, theme, LookSize.WIDE)

        look_pages.settle(page)
        login.submit().click()
        mouse = look_pages.outline(login.submit())
        if keyboard != LoginLook.RING:
            msg = (
                f"{theme}: Tab on the sign-in button draws outline {keyboard!r}, "
                f"expected the focus ring {LoginLook.RING!r}"
            )
            raise LookError(msg)

        if mouse != LoginLook.NO_RING:
            msg = (
                f"{theme}: a mouse click on the sign-in button draws outline "
                f"{mouse!r}, expected no ring {LoginLook.NO_RING!r}"
            )
            raise LookError(msg)

    look.verify()
