"""Вид карточки приложения MCP Apps в основной ленте: ход с приложением
заказов стандартного сервера под свёрнутым контейнером, раскрытый шаг
вызова без приложения, свёрнутая карточка, карточка на весь экран, два
приложения одного хода и тред, открытый заново, — в обеих темах и обоих
размерах окна.

Стенд вида поднимается с хостом приложений (AppsHost): стандартный сервер
по HTTP с секцией apps и страница песочницы на своём источнике со шрифтом
чата рядом. Вопросы — обычный текст: книга ответов фейковой модели зовёт по
ним orders (OrdersBook). Кадр ленты — видимая область прокрутки сообщений,
прокрученная к вопросу хода.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest
from acp_stand import AppsHost, OrdersBook
from aid_look import (
    LookCheck,
    LookError,
    LookPages,
    LookSignIn,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Error, FrameLocator, Locator, Page, WebSocket

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import ReplyBookClient
from boba.stand.ui.socket_log import SocketLog, StepField
from boba.stand.ui.stand import StandProcess, StandUrl

pytestmark = [pytest.mark.ui, pytest.mark.look]


class AppLook:
    """Состояния группы mcp-app и селекторы ленты."""

    GROUP: ClassVar[str] = "mcp-app"
    CARD: ClassVar[LookState] = LookState(
        GROUP,
        "ma1-card",
        "Ход с приложением: контейнер свёрнут, под ним карточка orders, ниже ответ",
    )
    STEP: ClassVar[LookState] = LookState(
        GROUP,
        "ma2-step",
        "Шаг вызова раскрыт: аргументы и итог, приложения в шаге нет; "
        "карточка под контейнером",
    )
    COLLAPSED: ClassVar[LookState] = LookState(
        GROUP, "ma3-collapsed", "Карточка свёрнута кнопкой: осталась шапка"
    )
    FULLSCREEN: ClassVar[LookState] = LookState(
        GROUP,
        "ma4-fullscreen",
        "Карточка на весь экран: та же шапка с кнопкой выхода, приложение на окно",
    )
    TWO_APPS: ClassVar[LookState] = LookState(
        GROUP,
        "ma5-two-apps",
        "Второй ход: два вызова — две карточки в порядке вызовов",
    )
    HISTORY: ClassVar[LookState] = LookState(
        GROUP,
        "ma6-history",
        "Тред открыт заново: карточка и приложение из истории агента",
    )

    FOLLOWING_CARDS: ClassVar[str] = "xpath=following::*[@data-mcp-app]"
    """Карточки приложений, идущие в ленте за сообщением."""
    FEED: ClassVar[str] = (
        "xpath=ancestor::div"
        "[contains(concat(' ', @class, ' '), ' overflow-y-auto ')][1]"
    )
    """Область прокрутки сообщений chainlit (ScrollContainer) от сообщения."""


class AppFeed:
    """Лента с приложением заказов глазами теста вида: вопросы книге ответов,
    карточки приложений, дорисованных с аргументами и итогом и со шрифтом
    чата, и область прокрутки сообщений, прокрученная к вопросу хода.
    Ошибки страницы чата копятся с открытия."""

    TOP: ClassVar[str] = "(node) => node.scrollIntoView({block: 'start'})"
    FONTS: ClassVar[str] = """async () => {
      await document.fonts.ready;
      const loaded = [...document.fonts].filter((face) =>
        face.family.replace(/['"]/g, "") === "Inter" && face.status === "loaded");
      return loaded.length > 0;
    }"""

    def __init__(self, page: Page, stand: StandProcess) -> None:
        self._page = page
        self._log = SocketLog()
        base = stand.config.base_url
        self._chat = ChatPage(page=page, log=self._log, base_url=base)
        self.errors: list[str] = []
        page.on("pageerror", self._failed)
        page.on("websocket", self._watch)

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def _watch(self, socket: WebSocket) -> None:
        socket.on("framereceived", self._log.accept)

    def reopen(self) -> None:
        """Тред открыт заново по адресу: лента собирается из истории агента."""
        step = self._log.last_step(StepKind.TOOL.value)
        if step is None:
            raise LookError(f"no tool step in the feed\n{self._log.describe()}")

        thread_id = str(step.get(StepField.THREAD_ID.value))
        self._page.goto(
            f"{self._chat.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        self._page.locator(LookSignIn.INPUT).wait_for(timeout=60_000)

    @property
    def chat(self) -> ChatPage:
        return self._chat

    def ask(self, question: str, answer: str, counts: tuple[str, ...]) -> None:
        """Ход с вопросом question: ответ answer и карточки с числом строк
        counts по порядку."""
        self._chat.ask(question)
        self._page.get_by_text(answer).last.wait_for(timeout=120_000)
        cards = self.turn(question).cards
        for index, count in enumerate(counts):
            self.drawn(cards.nth(index), count)

    def turn(self, question: str) -> FeedTurn:
        """Карточки хода с вопросом question: идут в ленте за его сообщением."""
        asked = self._page.locator(
            f'[data-step-type="{StepKind.USER.value}"]', has_text=question
        ).last
        cards = asked.locator(AppLook.FOLLOWING_CARDS)
        return FeedTurn(asked=asked, cards=cards)

    def drawn(self, card: Locator, count: str) -> None:
        """Приложение карточки получило строки, итог и шрифт чата."""
        inner = self.inner(card)
        inner.locator("#count").get_by_text(count).wait_for(timeout=60_000)
        inner.locator("#result").get_by_text("result:").wait_for(timeout=60_000)
        if not inner.locator("body").evaluate(self.FONTS):
            raise LookError(
                f"the app of {card} did not load the Inter font of the chat"
            )

    def inner(self, card: Locator) -> FrameLocator:
        return card.frame_locator("iframe").first.frame_locator("iframe")

    def feed(self, question: str) -> Locator:
        """Область прокрутки сообщений, прокрученная к вопросу хода."""
        asked = self.turn(question).asked
        asked.evaluate(self.TOP)
        return asked.locator(AppLook.FEED)

    def fold_last_run(self) -> None:
        """Сворачивает раскрытый контейнер последнего хода."""
        run = self._page.locator(Selector.of_type(StepKind.RUN.value)).last
        toggle = run.locator(Selector.STEP_TOGGLE.value).first
        if toggle.get_attribute(Selector.TOGGLE_STATE.value) != Selector.OPENED.value:
            return

        toggle.click()
        self._page.wait_for_timeout(300)

    def check_clean(self) -> None:
        if self.errors:
            raise LookError(f"the chat page raised errors: {self.errors}")


@dataclass(frozen=True)
class FeedTurn:
    """Ход в ленте: сообщение вопроса и карточки приложений за ним."""

    asked: Locator
    cards: Locator


@pytest.fixture(scope="module")
def apps_host(tmp_path_factory: pytest.TempPathFactory) -> Iterator[AppsHost]:
    host = AppsHost(Path(tmp_path_factory.mktemp("look-apps")))
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def apps_stand(look_stands: LookStand, apps_host: AppsHost) -> StandProcess:
    return look_stands.served("apps", {"apps": apps_host.drawn()})


def test_app_card_in_the_feed(
    look: LookCheck,
    look_pages: LookPages,
    apps_stand: StandProcess,
    look_llm_port: int,
) -> None:
    book = OrdersBook()
    ReplyBookClient(StandUrl.of(look_llm_port)).register(book.replies())
    for size in LookSize:
        for theme in LookTheme:
            page = look_pages.open(theme, size)
            feed = AppFeed(page, apps_stand)
            LookSignIn(page, apps_stand).sign_in()
            feed.ask(book.QUESTION, book.ANSWER, ("3 of 3 orders",))
            look_pages.settle(page)
            look.frame(feed.feed(book.QUESTION), AppLook.CARD, theme, size)

            feed.chat.expand_last_tool()
            look_pages.settle(page)
            look.frame(feed.feed(book.QUESTION), AppLook.STEP, theme, size)
            feed.fold_last_run()

            card = feed.turn(book.QUESTION).cards.first
            card.locator('[data-app-action="collapse"]').click()
            look_pages.settle(page)
            look.frame(feed.feed(book.QUESTION), AppLook.COLLAPSED, theme, size)
            card.locator('[data-app-action="expand"]').click()

            card.locator('[data-app-action="fullscreen"]').click()
            feed.inner(card).locator("#host").get_by_text("mode: fullscreen").wait_for()
            look_pages.settle(page)
            # карточка на весь экран и есть окно: боковая панель под ней
            # копит треды прогонов и в кадр не входит
            look.frame(card, AppLook.FULLSCREEN, theme, size)
            card.locator('[data-app-action="exit"]').click()

            feed.ask(
                book.TWO_STORES, book.TWO_ANSWER, ("2 of 2 orders", "1 of 1 orders")
            )
            look_pages.settle(page)
            look.frame(feed.feed(book.TWO_STORES), AppLook.TWO_APPS, theme, size)

            feed.reopen()
            reopened = feed.turn(book.QUESTION).cards.first
            reopened.wait_for(timeout=60_000)
            feed.drawn(reopened, "3 of 3 orders")
            look_pages.settle(page)
            look.frame(feed.feed(book.QUESTION), AppLook.HISTORY, theme, size)
            feed.check_clean()

    look.verify()
