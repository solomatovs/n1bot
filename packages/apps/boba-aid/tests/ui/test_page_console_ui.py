"""Консоль вкладки чата на обычном пути пользователя: открыть чат, спросить,
начать новый чат в другом профиле, переходить между тредами разных профилей и
перезагрузить вкладку на треде. Каждый шаг открывает ровно один сокет, а в
консоли нет ни ошибок, ни предупреждений — ни про accept поля загрузки, ни про
WebSocket, закрытый до установления.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from uuid import uuid4

import pytest
from chat_ui import OpenChat, PageWatch

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui]


class WalkControl(StrEnum):
    """Элементы фронта chainlit, по которым ходит тест."""

    THREAD_HISTORY = "#thread-history"
    NEW_CHAT = "#new-chat-button"
    CONFIRM = "#confirm"


class WalkTiming(IntEnum):
    """Задержка сети вкладки и пауза, за которую чат успокаивается после шага."""

    LATENCY_MS = 150
    SETTLE_MS = 3000


class ChatWalk:
    """Вкладка чата, которую тест ведёт по шагам пользователя.

    Сеть вкладки замедлена, как у пользователя выкатки: подключение сокета
    занимает заметное время, и лишнее переподключение закрывает сокет, не
    успевший открыться. Шаг ждёт, пока чат успокоится, и сверяет, сколько
    сокетов он открыл.
    """

    def __init__(self, chat: ChatPage) -> None:
        self.chat = chat
        self.watch = PageWatch(chat.page)

        conditions = {
            "offline": False,
            "latency": WalkTiming.LATENCY_MS.value,
            "downloadThroughput": -1,
            "uploadThroughput": -1,
        }

        cdp = chat.page.context.new_cdp_session(chat.page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", conditions)

    def reload(self, step: str) -> None:
        """Перезагрузка вкладки: один сокет новой страницы."""
        before = len(self.watch.sockets)

        self.chat.page.reload(wait_until="domcontentloaded")
        self.chat.page.wait_for_selector(Selector.INPUT.value)

        self._opened(step, before, 1)

    def ask(self, step: str, question: str) -> None:
        """Ход в текущем треде идёт по открытому сокету."""
        before = len(self.watch.sockets)

        self.chat.ask(f"{ScenarioName.ANSWER.value} {question}")
        self.chat.await_idle()

        self._opened(step, before, 0)

    def new_chat(self, step: str) -> None:
        before = len(self.watch.sockets)

        self.chat.page.locator(WalkControl.NEW_CHAT.value).click()
        self.chat.page.locator(WalkControl.CONFIRM.value).click()
        self.chat.page.wait_for_selector(Selector.INPUT.value)

        self._opened(step, before, 1)

    def switch_profile(self, step: str, name: str) -> None:
        before = len(self.watch.sockets)

        self.chat.open_profile_menu()
        self.chat.select_profile(name)

        self._opened(step, before, 1)

    def open_thread(self, step: str, question: str) -> None:
        """Тред из списка: один сокет, даже если профиль треда другой."""
        before = len(self.watch.sockets)

        history = self.chat.page.locator(WalkControl.THREAD_HISTORY.value)
        history.get_by_text(question).click()
        asked = self.chat.page.locator(Selector.of_type(StepKind.USER.value))
        asked.get_by_text(question).wait_for()

        self._opened(step, before, 1)

    def _opened(self, step: str, before: int, count: int) -> None:
        """Чат успокоился, и шаг открыл ровно count сокетов."""
        self.chat.page.wait_for_timeout(WalkTiming.SETTLE_MS.value)

        opened = self.watch.sockets[before:]
        if len(opened) != count:
            msg = (
                f"{step}: expected {count} socket(s) to open, got {len(opened)}: "
                f"{opened}\nconsole: {self.watch.complaints}"
            )
            raise AssertionError(msg)


def test_chat_console_stays_clean_across_threads_and_profiles(
    open_chat: OpenChat, stand: StandProcess
) -> None:
    general = f"console general {uuid4().hex[:8]}"
    search = f"console search {uuid4().hex[:8]}"
    walk = ChatWalk(open_chat(stand, ""))

    walk.reload("open the chat")
    walk.ask("ask in general", general)
    walk.new_chat("start a new chat")
    walk.switch_profile("switch to search", "search")
    walk.ask("ask in search", search)
    walk.open_thread("open the general thread", general)
    walk.open_thread("open the search thread", search)
    walk.reload("reload on the search thread")

    if walk.watch.complaints:
        shown = "\n".join(walk.watch.complaints[:20])
        msg = (
            f"the chat page logged {len(walk.watch.complaints)} error(s) or "
            f"warning(s), the first ones:\n{shown}"
        )
        raise AssertionError(msg)
