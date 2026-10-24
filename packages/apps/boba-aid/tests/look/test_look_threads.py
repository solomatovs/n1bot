"""Вид боковой панели тредов: группы по дню последней активности до хода и
после хода в старом треде, который поднимается в «Сегодня» первым, — в обеих
темах и обоих размерах окна.

Пользователь guest входит формой стенда с набором входов local; его треды
тест сеет в базу стендов вида сам, с днями активности относительно текущего
момента, поэтому группы кадра одни и те же в любой день. Другие тесты вида
guest не входят.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from aid_look import (
    LookCheck,
    LookFix,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Locator, expect
from test_look_conversation import TalkFeed, TalkLook, talk_book
from test_look_shell import ShellPage

from boba.stand.ui.chat_page import Selector, StepKind
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import ReplyBookClient, ScenarioName
from boba.stand.ui.stand import StandAuth

pytestmark = [pytest.mark.ui, pytest.mark.look]

__all__ = ["talk_book"]


class ThreadsFixes:
    """Правки вида, которые показывают кадры панели тредов."""

    ORDER: ClassVar[LookFix] = LookFix(
        code="Порядок тредов",
        summary=(
            "было: панель ставила треды по времени создания, ход в старом треде "
            "поднимал его наверх только в этой вкладке и до перезагрузки, а "
            "треды дальше первой страницы не подгружались; стало: треды стоят и "
            "группируются по последней записи истории агента, ход поднимает "
            "тред во всех вкладках пользователя, прокрутка подгружает следующие "
            "страницы"
        ),
    )


class ThreadsLook:
    """Состояния панели тредов и треды, которые тест сеет."""

    GROUP: ClassVar[str] = "threads"
    BEFORE: ClassVar[LookState] = LookState(
        GROUP,
        "th1-groups",
        "Треды по дням последней активности",
        "четыре группы сверху вниз: «Сегодня» — «План на сегодня», «Вчера» — "
        "«Разбор вчерашнего релиза», «Предыдущие 7 дней» — «Отчёт за неделю», "
        "«Предыдущие 30 дней» — «Архив задач»; ни один тред не выделен",
        (ThreadsFixes.ORDER,),
    )
    AFTER: ClassVar[LookState] = LookState(
        GROUP,
        "th2-after-turn",
        "Ход в старом треде поднял его наверх",
        "«Отчёт за неделю» первым в «Сегодня» и выделен, под ним «План на "
        "сегодня»; группы «Предыдущие 7 дней» нет, «Вчера» и «Предыдущие 30 "
        "дней» на местах",
        (ThreadsFixes.ORDER,),
    )

    LOGIN: ClassVar[str] = "guest"
    TODAY: ClassVar[str] = "План на сегодня"
    YESTERDAY: ClassVar[str] = "Разбор вчерашнего релиза"
    WEEK: ClassVar[str] = "Отчёт за неделю"
    MONTH: ClassVar[str] = "Архив задач"
    AGES: ClassVar[tuple[tuple[str, timedelta], ...]] = (
        (TODAY, timedelta(minutes=5)),
        (YESTERDAY, timedelta(days=1)),
        (WEEK, timedelta(days=3)),
        (MONTH, timedelta(days=20)),
    )
    QUESTION: ClassVar[str] = f"{ScenarioName.ANSWER.value} weekly status"
    THREADS: ClassVar[str] = '#thread-history [data-sidebar="menu-item"][id^="thread-"]'
    """Пункты тредов: у кнопки меню пункта тоже id thread-options."""


class ThreadsPanel:
    """Панель тредов вкладки теста вида: открыть, выбрать тред, спросить.
    Конец хода тест узнаёт по журналу сокета ленты разговора (TalkFeed)."""

    def __init__(self, feed: TalkFeed, shell: ShellPage, size: LookSize) -> None:
        self._feed = feed
        self._page = feed.page
        self._shell = shell
        self._size = size

    def shown(self) -> Locator:
        """Панель открыта, курсор увели с неё: кадр без подсветки под мышью."""
        viewport = self._size.viewport
        self._page.mouse.move(viewport["width"] - 10, viewport["height"] // 2)
        return self._shell.sidebar(self._size)

    def threads(self, panel: Locator) -> Locator:
        return panel.locator(ThreadsLook.THREADS)

    def ask_in(self, panel: Locator, name: str, question: str) -> None:
        """Тред name открывается кликом в панели, вопрос уходит, ход кончается."""
        self.threads(panel).filter(has_text=name).click()
        self._page.wait_for_url("**/thread/**", timeout=15_000)
        if self._size is LookSize.NARROW and panel.is_visible():
            self._shell.close(panel)

        expect(self._page.locator(Selector.INPUT.value)).to_be_visible(timeout=60_000)
        self._feed.chat.ask(question)
        self._feed.chat.await_idle(TalkLook.TURN_SEC)
        answers = self._page.locator(Selector.of_type(StepKind.ASSISTANT.value))
        expect(answers).to_have_count(1, timeout=60_000)


def test_thread_panel_follows_the_activity_look(
    look: LookCheck,
    look_pages: LookPages,
    look_stands: LookStand,
    look_db: StandDatabase,
    talk_book: ReplyBookClient,
) -> None:
    stand = look_stands.of(StandAuth.LOCAL)
    for size in LookSize:
        for theme in LookTheme:
            look_db.drop_threads_of(ThreadsLook.LOGIN)
            page = look_pages.open(theme, size)
            # журнал сокета слушает с открытия: сокет поднимает уже вход
            feed = TalkFeed(page, stand, talk_book)
            look_pages.sign_in(page, stand, ThreadsLook.LOGIN)
            now = datetime.now(UTC)
            for name, age in ThreadsLook.AGES:
                look_db.seed_thread(ThreadsLook.LOGIN, name, now - age)

            page.reload(wait_until="domcontentloaded")
            page.locator(Selector.INPUT.value).wait_for(timeout=60_000)
            page.wait_for_load_state("networkidle")
            shell = ShellPage(page, stand)
            threads = ThreadsPanel(feed, shell, size)

            panel = threads.shown()
            expect(threads.threads(panel)).to_contain_text(
                [
                    ThreadsLook.TODAY,
                    ThreadsLook.YESTERDAY,
                    ThreadsLook.WEEK,
                    ThreadsLook.MONTH,
                ]
            )
            look_pages.settle(page)
            look.frame(panel, ThreadsLook.BEFORE, theme, size)

            threads.ask_in(panel, ThreadsLook.WEEK, ThreadsLook.QUESTION)
            panel = threads.shown()
            expect(threads.threads(panel)).to_contain_text(
                [
                    ThreadsLook.WEEK,
                    ThreadsLook.TODAY,
                    ThreadsLook.YESTERDAY,
                    ThreadsLook.MONTH,
                ]
            )
            look_pages.settle(page)
            look.frame(panel, ThreadsLook.AFTER, theme, size)
            feed.check_clean()

    look_db.drop_threads_of(ThreadsLook.LOGIN)
    look.verify()
