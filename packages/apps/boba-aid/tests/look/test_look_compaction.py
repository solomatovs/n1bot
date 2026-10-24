"""Вид сжатия разговора: шаг хода «Сжимаю контекст…», пока агент
пересказывает разговор, и «Контекст сжат» с пересказом под шагом после — в
обеих темах и обоих размерах окна, вживую и после перезагрузки.

Стенд с малым окном модели (16 000 токенов): первый вопрос двумя большими
результатами инструмента large обычного сервера MCP стенда доводит
заполненность до порога, и второй вопрос начинается со сжатия. Учёт токенов
стенд считает по размеру запроса (`PUT /window`) и строже агента, поэтому
порог переходится ровно на втором вопросе. Запрос пересказа стоит на паузе
(`POST /summary/hold`), пока тест снимает кадр «сжимаю».
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import ClassVar

import pytest
from acp_stand import standard
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

from boba.mcp_client.client import McpServerConfig
from boba.stand.ui.chat_page import Selector, StepKind
from boba.stand.ui.fake_llm import (
    Reply,
    ReplyBookClient,
    ReplyCall,
    ReplyTurn,
    SummaryBook,
    WindowSettings,
)
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]

__all__ = ["standard", "talk_book"]


class CompactQuestion(StrEnum):
    """Вопросы разговора со сжатием: ключи книги ответов."""

    COLLECT = "Выгрузи заказы и возвраты за квартал"
    NEXT = "Что из этого стоит проверить первым?"


class CompactText(StrEnum):
    """Тексты ответов книги."""

    LOADING = "Выгружаю заказы и возвраты."
    COLLECTED = "Выгрузил заказы и возвраты: по две страницы на таблицу."
    NEXT = "Первыми стоит проверить возвраты: в них больше всего расхождений."


class CompactBook:
    """Книга: загрузка large поиском, два больших результата и ответ; на
    второй вопрос — ответ."""

    SERVER: ClassVar[str] = "mcp__standard__"
    LARGE: ClassVar[str] = "large"
    SEARCH: ClassVar[str] = "ToolSearch"
    SELECT: ClassVar[str] = "select:"
    SIZE: ClassVar[int] = 4_500
    WINDOW: ClassVar[int] = 16_000
    STAND_WINDOW: ClassVar[WindowSettings] = WindowSettings(
        context_window=1_000_000, chars_per_token=1
    )
    """Окно сервера без отказов, учёт — знак за токен: заполненность по
    учёту стенда выше оценки агента."""

    def replies(self) -> list[Reply]:
        large = ReplyCall(tool=self.full(self.LARGE), arguments={"size": self.SIZE})
        search = ReplyCall(
            tool=self.SEARCH,
            arguments={"query": f"{self.SELECT}{self.full(self.LARGE)}"},
        )
        return [
            Reply(
                question=CompactQuestion.COLLECT,
                turns=[
                    ReplyTurn(calls=[search]),
                    ReplyTurn(content=CompactText.LOADING, calls=[large]),
                    ReplyTurn(calls=[large]),
                    ReplyTurn(content=CompactText.COLLECTED),
                ],
            ),
            Reply(
                question=CompactQuestion.NEXT,
                turns=[ReplyTurn(content=CompactText.NEXT)],
            ),
        ]

    def full(self, tool: str) -> str:
        return f"{self.SERVER}{tool}"


class CompactFixes:
    STEP: ClassVar[LookFix] = LookFix(
        code="Сжатие контекста в ленте",
        summary=(
            "сжатие разговора — шаг хода: «Сжимаю контекст…» в подписи контейнера, "
            "пока агент пересказывает; после — «Контекст сжат» с пересказом под "
            "шагом, тот же после перезагрузки"
        ),
    )


class CompactLook:
    GROUP: ClassVar[str] = "conversation"
    COMPACTING: ClassVar[LookState] = LookState(
        GROUP,
        "cp1-compacting",
        "Агент сжимает разговор перед ответом",
        "под вопросом свёрнутый контейнер хода с подписью «○ Сжимаю контекст…», "
        "мерцает; кружок ожидания",
        (CompactFixes.STEP,),
    )
    FINISHED: ClassVar[LookState] = LookState(
        GROUP,
        "cp2-finished",
        "Ход после сжатия закончен",
        "контейнер свёрнут, подпись «Завершено · шагов: 1»; ниже ответ",
        (CompactFixes.STEP,),
    )
    OPEN: ClassVar[LookState] = LookState(
        GROUP,
        "cp3-open",
        "Контейнер хода раскрыт: шаг сжатия",
        "под контейнером шаг «✔ Контекст сжат», свёрнут",
        (CompactFixes.STEP,),
    )
    SUMMARY: ClassVar[LookState] = LookState(
        GROUP,
        "cp4-summary",
        "Шаг сжатия раскрыт: пересказ",
        "под шагом «✔ Контекст сжат» текст пересказа, который получила модель",
        (CompactFixes.STEP,),
    )
    FINISHED_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "cp2-finished-reloaded",
        "Тот же ход после перезагрузки",
        "кадр тот же, что «Ход после сжатия закончен»: сжатие пришло из истории",
        (CompactFixes.STEP,),
    )


@pytest.fixture(scope="module")
def compaction_stand(look_stands: LookStand, standard: McpServerConfig) -> StandProcess:
    server = standard.model_dump(mode="json", exclude_none=True)
    return look_stands.served(
        "compaction", {"standard": server}, context_window=CompactBook.WINDOW
    )


def compaction_frames(
    look_pages: LookPages, stand: StandProcess, book: ReplyBookClient
) -> Iterator[tuple[LookTheme, LookSize, TalkFeed]]:
    replies = CompactBook().replies()
    for size in LookSize:
        for theme in LookTheme:
            book.register(replies)
            page = look_pages.open(theme, size)
            feed = TalkFeed(page, stand, book)
            look_pages.sign_in(page, stand, TalkLook.LOGIN_USER)
            yield theme, size, feed


def region(feed: TalkFeed) -> Locator:
    asked = feed.page.locator(
        Selector.of_type(StepKind.USER.value), has_text=CompactQuestion.NEXT.value
    ).last
    asked.evaluate(LookPages.TOP)
    return asked.locator(TalkLook.FEED)


def test_compaction_look(
    look: LookCheck,
    look_pages: LookPages,
    compaction_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    talk_book.limit_window(CompactBook.STAND_WINDOW)
    try:
        for theme, size, feed in compaction_frames(
            look_pages, compaction_stand, talk_book
        ):
            page = feed.page
            feed.chat.ask(CompactQuestion.COLLECT.value)
            feed.chat.await_idle(TalkLook.TURN_SEC)
            feed.shown(CompactText.COLLECTED)
            feed.no_pulse()

            talk_book.hold_summary()
            feed.chat.ask(CompactQuestion.NEXT.value)
            talk_book.await_held(SummaryBook.HOLD_KEY, TalkLook.HELD_SEC)
            container = page.locator(Selector.of_type(StepKind.RUN.value)).last
            expect(container).to_contain_text("Сжимаю контекст")
            look_pages.settle(page)
            look.frame(region(feed), CompactLook.COMPACTING, theme, size)

            talk_book.release(SummaryBook.HOLD_KEY)
            feed.chat.await_idle(TalkLook.TURN_SEC)
            feed.shown(CompactText.NEXT)
            feed.no_pulse()
            look_pages.settle(page)
            look.frame(region(feed), CompactLook.FINISHED, theme, size)

            feed.open_last(StepKind.RUN)
            look_pages.settle(page)
            look.frame(region(feed), CompactLook.OPEN, theme, size)

            feed.open_last(StepKind.TOOL)
            look_pages.settle(page)
            look.frame(region(feed), CompactLook.SUMMARY, theme, size)

            feed.reload()
            feed.shown(CompactText.NEXT)
            look_pages.settle(page)
            look.frame(region(feed), CompactLook.FINISHED_AGAIN, theme, size)
            look.same_as(CompactLook.FINISHED_AGAIN, CompactLook.FINISHED, theme, size)
            feed.check_clean()
    finally:
        talk_book.reset()

    look.verify()
