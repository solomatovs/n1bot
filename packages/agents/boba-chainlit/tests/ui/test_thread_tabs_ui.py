"""Вкладки одного пользователя синхронизируются по шине: новый тред появляется в
списке другой вкладки без перезагрузки.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from chat_ui import ChatOpener
from playwright.sync_api import expect

from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui]

THREAD_HISTORY = "#thread-history"


def test_new_thread_appears_in_another_tab_through_the_bus(
    module_chats: ChatOpener, stand: StandProcess
) -> None:
    first = module_chats.open(stand)
    first.ask(f"{ScenarioName.ANSWER.value} thread list probe one")
    first.await_idle()
    expect(first.page.locator(THREAD_HISTORY)).to_contain_text("thread list probe one")

    second = module_chats.open(stand)
    second.ask(f"{ScenarioName.ANSWER.value} thread list probe two")
    second.await_idle()

    # первая вкладка узнаёт о новом треде из области пользователя, не перезагружаясь
    expect(first.page.locator(THREAD_HISTORY)).to_contain_text("thread list probe two")


def test_resumed_tab_learns_about_new_thread(
    module_chats: ChatOpener, stand: StandProcess
) -> None:
    """Вкладка, открытая на треде по прямой ссылке (resume), тоже видит новые треды."""
    probe_one = f"resumed probe {uuid4().hex[:8]}"
    probe_two = f"resumed probe {uuid4().hex[:8]}"

    first = module_chats.open(stand)
    first.ask(f"{ScenarioName.ANSWER.value} {probe_one}")
    first.await_idle()

    # прямая ссылка на тред превращает вкладку в resume-сессию
    first.page.locator(THREAD_HISTORY).get_by_text(probe_one).click()
    first.page.wait_for_url("**/thread/**", timeout=15000)
    thread_url = first.page.url
    first.page.goto(thread_url, wait_until="domcontentloaded")
    first.page.wait_for_selector("#chat-input", timeout=60000)
    expect(first.page.locator(THREAD_HISTORY)).to_contain_text(probe_one)

    second = module_chats.open(stand)
    second.ask(f"{ScenarioName.ANSWER.value} {probe_two}")
    second.await_idle()

    expect(first.page.locator(THREAD_HISTORY)).to_contain_text(probe_two)
