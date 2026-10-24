"""Вид сообщения агенту во время хода: поле ввода с кнопкой Stop у пустого
поля и Send у набранного, вопрос в очереди в ленте, его ход после текущего
ответа (тот же кадр после перезагрузки) и тексты очереди, вернувшиеся в
поле после Stop, — в обеих темах и обоих размерах окна.

Разговор, книга ответов и лента — те же, что у тестов вида разговора
(test_look_conversation): поток первого хода книга держит на паузе, пока
тест снимает кадры «идёт».
"""

from __future__ import annotations

import time
from typing import ClassVar

import pytest
from aid_look import LookCheck, LookFix, LookPages, LookState
from playwright.sync_api import Locator, expect
from test_look_conversation import (
    TalkBook,
    TalkFeed,
    TalkLook,
    TalkQuestion,
    TalkText,
    talk_book,
    talk_frames,
    talk_stand,
)

from boba.stand.ui.chat_page import Selector, StepKind
from boba.stand.ui.fake_llm import ReplyBookClient
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]

__all__ = ["talk_book", "talk_stand"]


class QueuedFixes:
    """Правки вида, которые показывают кадры очереди."""

    INPUT: ClassVar[LookFix] = LookFix(
        code="Сообщение во время хода",
        summary=(
            "было: пока идёт ход, поле ввода заперто, у него только кнопка Stop, "
            "написать агенту можно было, лишь остановив ход; стало: поле открыто, "
            "у пустого поля Stop, у набранного — Send, отправленное встаёт в "
            "очередь агента и получает свой ход после текущего ответа, Stop "
            "возвращает тексты очереди в поле"
        ),
    )


class QueuedLook:
    """Состояния поля ввода и ленты с очередью."""

    GROUP: ClassVar[str] = "queued"
    STOP_EMPTY: ClassVar[LookState] = LookState(
        GROUP,
        "qi1-stop-empty",
        "Идёт ход, поле пустое: кнопка Stop",
        "поле ввода открыто, с подсказкой; справа круглая кнопка Stop "
        "(квадрат), кнопки Send нет; кнопки вложений не серые",
        (QueuedFixes.INPUT,),
    )
    SEND_TYPED: ClassVar[LookState] = LookState(
        GROUP,
        "qi2-send-typed",
        "Идёт ход, в поле набран текст: кнопка Send",
        "в поле текст вопроса; справа круглая кнопка Send (стрелка), кнопки Stop нет",
        (QueuedFixes.INPUT,),
    )
    QUEUED: ClassVar[LookState] = LookState(
        GROUP,
        "qi3-queued",
        "Вопрос отправлен во время хода и ждёт в очереди",
        "вопрос первого хода наверху, его ответ остановился на двенадцати "
        "словах и мигает курсором; ниже — пузырь отправленного вопроса справа",
        (QueuedFixes.INPUT,),
    )
    ANSWERED: ClassVar[LookState] = LookState(
        GROUP,
        "qi4-answered",
        "Вопрос из очереди получил свой ход после первого ответа",
        "вопрос первого хода наверху, его ответ целиком, под ним пузырь "
        "вопроса из очереди и ответ на него; кружка и курсора нет; на узком "
        "окне пузырь вопроса из очереди внизу, ниже видна кнопка «↓»",
        (QueuedFixes.INPUT,),
    )
    ANSWERED_AGAIN: ClassVar[LookState] = LookState(
        GROUP,
        "qi4-answered-reloaded",
        "Та же лента после перезагрузки страницы",
        "кадр тот же, что «Вопрос из очереди получил свой ход»; тест сверил "
        "его с живым пиксель в пиксель",
        (QueuedFixes.INPUT,),
    )
    RESTORED: ClassVar[LookState] = LookState(
        GROUP,
        "qi5-restored",
        "Stop снял очередь: её тексты вернулись в поле",
        "в поле два вопроса очереди по порядку отправки через пустую строку; "
        "справа кнопка Send; в ленте этих вопросов нет",
        (QueuedFixes.INPUT,),
    )
    QUEUED_LOG: ClassVar[str] = "goes to the queue of session"
    QUEUE_SEC: ClassVar[float] = 60.0
    SETTLE_ROUNDS: ClassVar[int] = 40
    QUIET_ROUNDS: ClassVar[int] = 3
    SETTLE_MS: ClassVar[int] = 250


class QueuedInput:
    """Поле ввода и очередь глазами теста вида."""

    def __init__(self, feed: TalkFeed, stand: StandProcess) -> None:
        self._feed = feed
        self._stand = stand
        self._page = feed.page

    @property
    def composer(self) -> Locator:
        return self._page.locator(TalkLook.COMPOSER_BOX)

    @property
    def field(self) -> Locator:
        return self._page.locator(Selector.INPUT.value)

    def queued_so_far(self) -> int:
        """Строки лога стенда о вопросах, поставленных в очередь."""
        marker = QueuedLook.QUEUED_LOG.encode()
        found = 0
        with self._stand.log_path.open("rb") as handle:
            for line in handle:
                if marker in line:
                    found += 1

        return found

    def send(self, question: TalkQuestion) -> None:
        """Вопрос во время хода: Enter в поле, сервер ставит его в очередь."""
        before = self.queued_so_far()
        self.field.fill(question.value)
        self.field.press("Enter")
        expect(self.field).to_have_value("")
        deadline = time.monotonic() + QueuedLook.QUEUE_SEC
        while self.queued_so_far() <= before:
            if time.monotonic() > deadline:
                msg = f"{question.value!r} did not reach the agent queue"
                raise AssertionError(msg)

            time.sleep(0.1)

    def feed(self, question: TalkQuestion) -> Locator:
        """Лента, прокрученная к вопросу хода, когда кнопка «↓» пришла в
        покой: chainlit показывает её позже прокрутки, поэтому кадр ждёт,
        пока её наличие не повторится QUIET_ROUNDS раз подряд."""
        region = self._feed.feed(question)
        button = region.locator(TalkLook.SCROLL_DOWN)
        last = -1
        same = 0
        for _ in range(QueuedLook.SETTLE_ROUNDS):
            shown = button.count()
            same += 1
            if shown != last:
                same = 0

            if same >= QueuedLook.QUIET_ROUNDS:
                return region

            last = shown
            self._page.wait_for_timeout(QueuedLook.SETTLE_MS)

        msg = f"the scroll-down button of the feed at {question.value!r} never settled"
        raise AssertionError(msg)

    def bubble(self, question: TalkQuestion) -> Locator:
        return self._page.locator(
            Selector.of_type(StepKind.USER.value), has_text=question.value
        )


def test_message_during_the_turn_look(
    look: LookCheck,
    look_pages: LookPages,
    talk_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    book = TalkBook()
    streamed = book.streamed(book.words(80, TalkBook.STEPS_SEED), TalkBook.STEPS_TOKENS)
    for theme, size, feed in talk_frames(look_pages, talk_stand, talk_book):
        page = feed.page
        queue = QueuedInput(feed, talk_stand)

        feed.ask_held(TalkQuestion.STEPS)
        expect(feed.answer()).to_contain_text(streamed, timeout=30_000)
        expect(page.locator(TalkLook.STOP)).to_be_visible()
        look_pages.settle(page)
        look.frame(queue.composer, QueuedLook.STOP_EMPTY, theme, size)

        queue.field.fill(TalkQuestion.STATUS.value)
        expect(page.locator(Selector.SUBMIT.value)).to_be_enabled()
        look_pages.settle(page)
        look.frame(queue.composer, QueuedLook.SEND_TYPED, theme, size)

        queue.field.fill("")
        queue.send(TalkQuestion.STATUS)
        expect(queue.bubble(TalkQuestion.STATUS)).to_be_visible()
        look_pages.settle(page)
        look.frame(queue.feed(TalkQuestion.STEPS), QueuedLook.QUEUED, theme, size)

        feed.release(TalkQuestion.STEPS)
        feed.shown(TalkText.STATUS)
        feed.no_pulse()
        look_pages.settle(page)
        look.frame(queue.feed(TalkQuestion.STEPS), QueuedLook.ANSWERED, theme, size)

        feed.reload()
        feed.shown(TalkText.STATUS)
        look_pages.settle(page)
        look.frame(
            queue.feed(TalkQuestion.STEPS), QueuedLook.ANSWERED_AGAIN, theme, size
        )
        look.same_as(QueuedLook.ANSWERED_AGAIN, QueuedLook.ANSWERED, theme, size)

        feed.ask_held(TalkQuestion.PLAN)
        queue.send(TalkQuestion.DAY)
        queue.send(TalkQuestion.WEEK)
        feed.stop(TalkQuestion.PLAN)
        restored = f"{TalkQuestion.DAY.value}\n\n{TalkQuestion.WEEK.value}"
        expect(queue.field).to_have_value(restored, timeout=30_000)
        expect(queue.bubble(TalkQuestion.DAY)).to_have_count(0)
        expect(queue.bubble(TalkQuestion.WEEK)).to_have_count(0)
        look_pages.settle(page)
        look.frame(queue.composer, QueuedLook.RESTORED, theme, size)
        feed.check_clean()

    look.verify()
