"""Порядок тредов в боковой панели — по последней записи истории агента: ход
поднимает свой тред наверх во всех вкладках пользователя без перезагрузки,
перезагрузка показывает тот же порядок, открытие старого треда его не меняет;
прокрутка панели подгружает следующие страницы без повторов и пропусков.

Тесты входят пользователем guest, тредов которого больше никто не трогает:
панель каждого теста начинается пустой.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import uuid4

import pytest
from chat_ui import ChatOpener
from playwright.sync_api import Locator, expect

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui]


class ThreadPanel:
    """Боковая панель тредов вкладки глазами теста."""

    LOGIN: ClassVar[str] = "guest"
    THREADS: ClassVar[str] = '#thread-history [data-sidebar="menu-item"][id^="thread-"]'
    """Пункты тредов: у кнопки меню пункта тоже id thread-options."""
    CONTENT: ClassVar[str] = '[data-sidebar="content"]'
    PAGE: ClassVar[int] = 35
    """BATCH_SIZE страницы панели chainlit."""

    def __init__(self, chat: ChatPage) -> None:
        self._page = chat.page

    @property
    def threads(self) -> Locator:
        return self._page.locator(self.THREADS)

    def shows(self, names: list[str]) -> None:
        """Панель показывает ровно эти треды в этом порядке."""
        expect(self.threads).to_have_count(len(names))
        expect(self.threads).to_contain_text(names)

    def open(self, name: str, asked: str) -> None:
        """Тред открывается кликом в панели, и лента воспроизводит его вопрос."""
        self.threads.filter(has_text=name).click()
        self._page.wait_for_url("**/thread/**", timeout=15_000)
        expect(self._page.locator(Selector.INPUT.value)).to_be_visible(timeout=60_000)
        question = self._page.locator(Selector.of_type(StepKind.USER.value))
        expect(question.filter(has_text=asked)).to_have_count(1, timeout=60_000)

    def scroll_down(self) -> None:
        self._page.locator(self.CONTENT).evaluate(
            "content => { content.scrollTop = content.scrollHeight; "
            "content.dispatchEvent(new Event('scroll')); }"
        )


def test_a_turn_moves_its_thread_up_in_every_tab(
    module_chats: ChatOpener, stand: StandProcess, stand_db: StandDatabase
) -> None:
    stand_db.drop_threads_of(ThreadPanel.LOGIN)
    marker = uuid4().hex[:8]
    older = f"{ScenarioName.ANSWER.value} older probe {marker}"
    newer = f"{ScenarioName.ANSWER.value} newer probe {marker}"

    first = module_chats.open(stand, ThreadPanel.LOGIN)
    first.ask(older)
    first.await_idle()
    first.open()
    first.ask(newer)
    first.await_idle()
    panel = ThreadPanel(first)
    panel.shows([newer, older])

    other = module_chats.open(stand, ThreadPanel.LOGIN)
    watched = ThreadPanel(other)
    watched.shows([newer, older])

    # открытие и воспроизведение старого треда порядок не меняют
    panel.open(older, f"older probe {marker}")
    panel.shows([newer, older])
    watched.shows([newer, older])

    first.ask(f"{ScenarioName.ANSWER.value} again {marker}")
    first.await_idle()
    panel.shows([older, newer])
    watched.shows([older, newer])

    other.open()
    watched.shows([older, newer])


def test_scrolling_the_panel_loads_every_thread_once(
    module_chats: ChatOpener, stand: StandProcess, stand_db: StandDatabase
) -> None:
    chat = module_chats.open(stand, ThreadPanel.LOGIN)
    stand_db.drop_threads_of(ThreadPanel.LOGIN)
    marker = uuid4().hex[:8]
    now = datetime.now(UTC)
    names: list[str] = []
    for index in range(ThreadPanel.PAGE + 5):
        name = f"page probe {marker} {index:02d}"
        stand_db.seed_thread(ThreadPanel.LOGIN, name, now - timedelta(minutes=index))
        names.append(name)

    chat.open()
    panel = ThreadPanel(chat)
    expect(panel.threads).to_have_count(ThreadPanel.PAGE)
    expect(panel.threads).to_contain_text(names[: ThreadPanel.PAGE])

    panel.scroll_down()
    panel.shows(names)
    stand_db.drop_threads_of(ThreadPanel.LOGIN)
