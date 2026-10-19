"""Вид оболочки чата: пустой тред, шапка, боковая панель, меню темы,
профилей и пользователя, диалоги «Новый чат» и «Описание» — в обеих темах и
обоих размерах окна. Клавиатурный фокус кнопок шапки, боковой панели и поля
ввода снимается на широком окне в обеих темах.

Пользователь dev входит формой стенда с одним набором входов (local) и своей
базой тестов вида. Другие тесты вида входят admin и оставляют треды в общей
базе, dev не входит никто: тредов у него нет, боковая панель пуста, и тест
это проверяет. На узком окне боковая панель — выезжающий лист, её открывает
кнопка шапки.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from aid_look import (
    LookCheck,
    LookError,
    LookFixes,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Locator, Page, expect

from boba.stand.ui.stand import StandAuth, StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]


class ShellLook:
    """Состояния оболочки (SH плана тестов вида) и селекторы chainlit."""

    GROUP: ClassVar[str] = "shell"
    EMPTY: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-empty-chat",
        "Пустой тред: логотип, поле ввода, шапка и боковая панель — страница целиком",
        "логотип N1 над полем ввода (в тёмной теме белый, «1» оранжевая); "
        "подсказка поля и подписи по-русски; ни у одного контрола нет кольца "
        "фокуса; на узком окне боковая панель скрыта, прокрутки вбок нет",
        (),
    )
    HEADER: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-header",
        "Шапка: боковая панель, «Новый чат», профиль, описание, тема, пользователь",
        "профиль по умолчанию «Общий» с русским названием; «Описание», "
        "переключатель темы и кружок пользователя выровнены по высоте",
        (LookFixes.N3,),
    )
    SIDEBAR: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-sidebar",
        "Боковая панель без тредов (узкое окно — лист)",
        "кнопки боковой панели, поиска и нового чата на месте, вместо списка "
        "тредов — плашка «Чаты не найдены»",
        (),
    )
    THEME_MENU: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-theme-menu",
        "Меню темы: светлая, тёмная, системная",
        "три пункта по-русски: «Светлая тема», «Тёмная тема», «Как в системе»",
        (),
    )
    PROFILES: ClassVar[LookState] = LookState(
        GROUP,
        "sh2-profiles",
        "Меню профилей чата раскрыто",
        "пункты «Общий» и «Поиск» по-русски, галочка у выбранного «Общий»",
        (LookFixes.N3,),
    )
    NEW_CHAT: ClassVar[LookState] = LookState(
        GROUP,
        "sh5-new-chat",
        "Подтверждение «Новый чат»",
        "заголовок, объяснение и кнопки «Отмена» и «Подтвердить» по-русски, "
        "основная кнопка в цвете темы",
        (),
    )
    README: ClassVar[LookState] = LookState(
        GROUP,
        "sh6-readme",
        "Описание приложения (chainlit.md)",
        "заголовок описания — «Ассистент-BOBA», как имя ассистента в конфиге",
        (LookFixes.NAME,),
    )
    USER_MENU: ClassVar[LookState] = LookState(
        GROUP,
        "sh7-user-menu",
        "Меню пользователя",
        "имя пользователя и «Выйти» со значком выхода",
        (),
    )
    FOCUS_HEADER: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-focus-header",
        "Tab с клавиатуры встал на «Описание» в шапке",
        "кольцо 2 px цвета темы вокруг «Описание» и только вокруг неё; в "
        "светлой теме синий Consta, в тёмной — голубое",
        (LookFixes.FOCUS,),
    )
    FOCUS_SIDEBAR: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-focus-sidebar",
        "Tab с клавиатуры встал на поиск тредов в боковой панели",
        "кольцо вокруг кнопки поиска видно целиком, край панели его не режет; "
        "под кнопкой подсказка «Поиск» с сочетанием клавиш — её открывает фокус",
        (LookFixes.FOCUS,),
    )
    FOCUS_COMPOSER: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-focus-composer",
        "Tab с клавиатуры встал на вложение файла в поле ввода",
        "кольцо вокруг скрепки вложения внутри поля ввода и над ней подсказка "
        "«Прикрепить файлы», у самого поля рамки нет; шестерёнки настроек в поле "
        "ввода нет",
        (LookFixes.FOCUS,),
    )

    LOGIN: ClassVar[str] = "#email"
    PASSWORD: ClassVar[str] = "#password"
    SUBMIT: ClassVar[str] = 'form button[type="submit"]'
    INPUT: ClassVar[str] = "#chat-input"
    HEADER_BOX: ClassVar[str] = "#header"
    SIDEBAR_BOX: ClassVar[str] = '[data-sidebar="sidebar"]'
    SIDEBAR_TRIGGER: ClassVar[str] = "#sidebar-trigger-button"
    THEME: ClassVar[str] = "#theme-toggle"
    PROFILE_SELECT: ClassVar[str] = "#chat-profiles"
    NEW_CHAT_BUTTON: ClassVar[str] = "#new-chat-button"
    NEW_CHAT_DIALOG: ClassVar[str] = "#new-chat-dialog"
    README_BUTTON: ClassVar[str] = "#readme-button"
    USER_BUTTON: ClassVar[str] = "#user-nav-button"
    MENU: ClassVar[str] = '[role="menu"]'
    LISTBOX: ClassVar[str] = '[role="listbox"]'
    DIALOG: ClassVar[str] = '[role="dialog"]'
    COMPOSER: ClassVar[str] = "#message-composer"
    SEARCH: ClassVar[str] = "#search-chats-button"
    UPLOAD: ClassVar[str] = "#upload-button"
    RING: ClassVar[str] = "solid 2px"
    LOGIN_USER: ClassVar[str] = "dev"
    THREADS: ClassVar[str] = '#thread-history [id^="thread-"]'
    ASIDE_X: ClassVar[int] = 20
    ASIDE_Y: ClassVar[int] = 500


class ShellPage:
    """Оболочка чата глазами теста вида: вход формой и раскрытие меню."""

    def __init__(self, page: Page, stand: StandProcess) -> None:
        self._page = page
        self._stand = stand

    def sign_in(self) -> None:
        credential = self._stand.config.credential(ShellLook.LOGIN_USER)
        base = self._stand.config.base_url
        self._page.goto(f"{base}/login", wait_until="domcontentloaded")
        self._page.locator(ShellLook.LOGIN).fill(credential.login)
        self._page.locator(ShellLook.PASSWORD).fill(credential.password)
        self._page.locator(ShellLook.SUBMIT).click()
        expect(self._page.locator(ShellLook.INPUT)).to_be_visible(timeout=60_000)
        expect(self._page.locator(ShellLook.PROFILE_SELECT)).to_be_visible()
        self._page.wait_for_load_state("networkidle")
        expect(self._page.locator(ShellLook.THREADS)).to_have_count(0)

    def opened(self, trigger: str, overlay: str) -> Locator:
        """Нажимает кнопку и отдаёт раскрытое ею меню или диалог."""
        self._page.locator(trigger).click()
        shown = self._page.locator(overlay).last
        expect(shown).to_be_visible()
        return shown

    def close(self, overlay: Locator) -> None:
        """Закрывает меню или диалог: Escape, а список профилей, который его не
        слушает, — кликом мимо, как ChatPage.close_profile_menu."""
        self._page.keyboard.press("Escape")
        if overlay.is_visible():
            self._page.mouse.click(ShellLook.ASIDE_X, ShellLook.ASIDE_Y)

        expect(overlay).to_be_hidden()

    def sidebar(self, size: LookSize) -> Locator:
        """Боковая панель: на широком окне открыта сама, на узком — листом."""
        panel = self._page.locator(ShellLook.SIDEBAR_BOX).last
        if size is LookSize.NARROW:
            self._page.locator(ShellLook.SIDEBAR_TRIGGER).click()

        expect(panel).to_be_visible()
        return panel


def test_shell_of_an_empty_chat(
    look: LookCheck, look_pages: LookPages, look_stands: LookStand
) -> None:
    stand = look_stands.of(StandAuth.LOCAL)
    for size in LookSize:
        for theme in LookTheme:
            page = look_pages.open(theme, size)
            shell = ShellPage(page, stand)
            shell.sign_in()
            look_pages.settle(page)

            look.frame(page.locator("body"), ShellLook.EMPTY, theme, size)
            header = page.locator(ShellLook.HEADER_BOX)
            look.frame(header, ShellLook.HEADER, theme, size)

            sidebar = shell.sidebar(size)
            look_pages.settle(page)
            look.frame(sidebar, ShellLook.SIDEBAR, theme, size)
            if size is LookSize.NARROW:
                shell.close(sidebar)

            overlays = (
                (ShellLook.THEME, ShellLook.MENU, ShellLook.THEME_MENU),
                (ShellLook.PROFILE_SELECT, ShellLook.LISTBOX, ShellLook.PROFILES),
                (
                    ShellLook.NEW_CHAT_BUTTON,
                    ShellLook.NEW_CHAT_DIALOG,
                    ShellLook.NEW_CHAT,
                ),
                (ShellLook.README_BUTTON, ShellLook.DIALOG, ShellLook.README),
                (ShellLook.USER_BUTTON, ShellLook.MENU, ShellLook.USER_MENU),
            )
            for trigger, overlay, state in overlays:
                shown = shell.opened(trigger, overlay)
                look.frame(shown, state, theme, size)
                shell.close(shown)

    look.verify()


def test_shell_keyboard_focus(
    look: LookCheck, look_pages: LookPages, look_stands: LookStand
) -> None:
    """Tab рисует кольцо фокуса темы у кнопок шапки, боковой панели и поля
    ввода; что клик мышью кольца не рисует, проверяет вход."""
    stand = look_stands.of(StandAuth.LOCAL)
    for theme in LookTheme:
        page = look_pages.open(theme, LookSize.WIDE)
        shell = ShellPage(page, stand)
        shell.sign_in()
        look_pages.settle(page)

        targets = (
            (ShellLook.README_BUTTON, ShellLook.HEADER_BOX, ShellLook.FOCUS_HEADER),
            (ShellLook.SEARCH, ShellLook.SIDEBAR_BOX, ShellLook.FOCUS_SIDEBAR),
            (ShellLook.UPLOAD, ShellLook.COMPOSER, ShellLook.FOCUS_COMPOSER),
        )
        for target, region, state in targets:
            look_pages.tab_to(page, target)
            keyboard = look_pages.outline(page.locator(target).first)
            look.frame(page.locator(region).last, state, theme, LookSize.WIDE)
            if keyboard != ShellLook.RING:
                msg = (
                    f"{theme}: Tab on {target} draws outline {keyboard!r}, "
                    f"expected the focus ring {ShellLook.RING!r}"
                )
                raise LookError(msg)

    look.verify()
