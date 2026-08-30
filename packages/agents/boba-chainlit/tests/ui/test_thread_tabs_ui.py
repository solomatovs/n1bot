"""Вкладки одного пользователя синхронизируются по шине: новый тред появляется в
списке другой вкладки без перезагрузки.
"""

from __future__ import annotations

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
