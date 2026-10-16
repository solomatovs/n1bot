"""Вкладки одного пользователя синхронизируются по шине: новый тред появляется в
списке другой вкладки без перезагрузки, правка вопроса в одной вкладке убирает
хвост беседы в другой.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from chat_ui import ChatOpener
from playwright.sync_api import expect

from boba.stand.ui.chat_page import Selector, StepKind
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


def test_an_edit_in_one_tab_removes_the_tail_in_another(
    module_chats: ChatOpener, stand: StandProcess
) -> None:
    """Вторая вкладка треда после правки первого вопроса в первой: хвоста
    беседы нет, правленый вопрос есть."""
    marker = uuid4().hex[:8]
    first = f"{ScenarioName.ANSWER.value} edit probe {marker}"
    tail = f"{ScenarioName.ANSWER.value} tail probe {marker}"
    edited = f"{ScenarioName.ANSWER.value} edited probe {marker}"

    editing = module_chats.open(stand)
    editing.ask(first)
    editing.await_idle()
    editing.ask(tail)
    editing.await_idle()
    editing.page.locator(THREAD_HISTORY).get_by_text(f"edit probe {marker}").click()
    editing.page.wait_for_url("**/thread/**", timeout=15000)
    thread_url = editing.page.url
    editing.page.wait_for_selector(Selector.INPUT.value, timeout=60000)

    other = module_chats.open(stand)
    other.page.goto(thread_url, wait_until="domcontentloaded")
    other.page.wait_for_selector(Selector.INPUT.value, timeout=60000)
    questions = other.page.locator(Selector.of_type(StepKind.USER.value))
    expect(questions.filter(has_text=f"tail probe {marker}")).to_have_count(1)

    editing.page.locator(".edit-message").first.click(force=True)
    editing.page.locator("#edit-chat-input").fill(edited)
    editing.page.locator(".confirm-edit").click()
    editing.await_idle()

    expect(questions.filter(has_text=f"edited probe {marker}")).to_have_count(1)
    expect(questions.filter(has_text=f"tail probe {marker}")).to_have_count(0)
