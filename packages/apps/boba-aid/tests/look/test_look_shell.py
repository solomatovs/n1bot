"""Вид оболочки чата: пустой тред, шапка, боковая панель, меню темы,
профилей и пользователя, диалоги «Новый чат» и «Описание» — в обеих темах и
обоих размерах окна.

Пользователь входит формой стенда с одним набором входов (local) и своей
базой тестов вида: тредов у него нет, боковая панель пуста. На узком окне
боковая панель — выезжающий лист, её открывает кнопка шапки.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from aid_look import (
    LookCheck,
    LookPages,
    LookSignIn,
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
    )
    HEADER: ClassVar[LookState] = LookState(
        GROUP,
        "sh1-header",
        "Шапка: боковая панель, «Новый чат», профиль, описание, тема, пользователь",
    )
    SIDEBAR: ClassVar[LookState] = LookState(
        GROUP, "sh1-sidebar", "Боковая панель без тредов (узкое окно — лист)"
    )
    THEME_MENU: ClassVar[LookState] = LookState(
        GROUP, "sh1-theme-menu", "Меню темы: светлая, тёмная, системная"
    )
    PROFILES: ClassVar[LookState] = LookState(
        GROUP, "sh2-profiles", "Меню профилей чата раскрыто"
    )
    NEW_CHAT: ClassVar[LookState] = LookState(
        GROUP, "sh5-new-chat", "Подтверждение «Новый чат»"
    )
    README: ClassVar[LookState] = LookState(
        GROUP, "sh6-readme", "Описание приложения (chainlit.md)"
    )
    USER_MENU: ClassVar[LookState] = LookState(
        GROUP, "sh7-user-menu", "Меню пользователя"
    )

    HEADER_BOX: ClassVar[str] = "#header"
    SIDEBAR_BOX: ClassVar[str] = '[data-sidebar="sidebar"]'
    SIDEBAR_TRIGGER: ClassVar[str] = "#sidebar-trigger-button"
    THEME: ClassVar[str] = "#theme-toggle"
    PROFILE_SELECT: ClassVar[str] = LookSignIn.PROFILE_SELECT
    NEW_CHAT_BUTTON: ClassVar[str] = "#new-chat-button"
    NEW_CHAT_DIALOG: ClassVar[str] = "#new-chat-dialog"
    README_BUTTON: ClassVar[str] = "#readme-button"
    USER_BUTTON: ClassVar[str] = "#user-nav-button"
    MENU: ClassVar[str] = '[role="menu"]'
    LISTBOX: ClassVar[str] = '[role="listbox"]'
    DIALOG: ClassVar[str] = '[role="dialog"]'
    ASIDE_X: ClassVar[int] = 20
    ASIDE_Y: ClassVar[int] = 500


class ShellPage:
    """Оболочка чата глазами теста вида: вход формой и раскрытие меню."""

    def __init__(self, page: Page, stand: StandProcess) -> None:
        self._page = page
        self._stand = stand

    def sign_in(self) -> None:
        LookSignIn(self._page, self._stand).sign_in()

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
