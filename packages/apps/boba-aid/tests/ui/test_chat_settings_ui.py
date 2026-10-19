"""Панели настроек агента в браузере нет: обращение к модели задаёт профиль
чата, пользователю его не показывают. Ни новый чат, ни ход, открывший сессию
агента, ни возврат к треду по адресу его страницы не присылают вкладке
панель (событие chat_settings), и кнопки настроек у поля ввода нет — ни в
профиле general, ни в search.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from playwright.sync_api import expect

from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.socket_log import ChatEvent

pytestmark = pytest.mark.ui


class SettingsPanel(StrEnum):
    """Селекторы chainlit, по которым видна панель настроек."""

    OPEN_MODAL = "#chat-settings-open-modal"
    INPUT = "#chat-input"


class TestNoSettingsPanel:
    """Вкладка не получает панели настроек ни на одном шаге жизни треда."""

    def test_no_panel_before_and_after_the_first_turn(self, chat: ChatPage) -> None:
        self._assert_no_panel(chat)

        self._ask(chat)

        self._assert_no_panel(chat)

    def test_no_panel_after_the_thread_is_resumed(self, chat: ChatPage) -> None:
        self._ask(chat)

        # открытие страницы треда идёт возвратом к нему: история воспроизводится
        thread_url = f"{chat.base_url}/thread/{chat.log.thread_id()}"
        chat.page.goto(thread_url, wait_until="domcontentloaded")
        chat.page.wait_for_selector(SettingsPanel.INPUT.value, timeout=60000)
        chat.await_step(StepKind.ASSISTANT.value)
        # панель приходила сразу за воспроизведением истории
        chat.page.wait_for_timeout(1000)

        self._assert_no_panel(chat)

    def test_search_profile_has_no_panel_either(self, chat: ChatPage) -> None:
        chat.open_profile_menu()
        chat.select_profile("search")

        self._ask(chat)

        self._assert_no_panel(chat)

    @staticmethod
    def _ask(chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.ANSWER.value} please")
        chat.await_step(StepKind.ASSISTANT.value)
        chat.await_idle()

    @staticmethod
    def _assert_no_panel(chat: ChatPage) -> None:
        expect(chat.page.locator(SettingsPanel.OPEN_MODAL.value)).to_have_count(0)

        panels = chat.log.of_event(ChatEvent.CHAT_SETTINGS)
        if panels:
            msg = f"the tab got {len(panels)} settings panel(s): {chat.log.describe()}"
            raise AssertionError(msg)
