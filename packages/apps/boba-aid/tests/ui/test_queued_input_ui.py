"""Сообщение агенту во время хода: поле ввода открыто, у пустого поля кнопка
Stop, у набранного — Send; отправленное встаёт в очередь сессии агента и
получает свой ход после текущего ответа, а Stop снимает очередь и
возвращает её тексты в поле ввода. Поток первого хода книга ответов держит
на паузе, пока тест смотрит на поле и ленту.
"""

from __future__ import annotations

import time
from typing import ClassVar

import pytest
from playwright.sync_api import Locator, expect

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import Reply, ReplyBookClient, ReplyTurn
from boba.stand.ui.stand import StandProcess, StandUrl

pytestmark = pytest.mark.ui


class QueueBook:
    """Вопросы книги этого модуля и ответы на них."""

    HELD: ClassVar[str] = "Describe the migration steps slowly"
    HELD_ANSWER: ClassVar[str] = "Freeze the schema, move the data and compare counts."
    HELD_TOKENS: ClassVar[int] = 3
    QUEUED: ClassVar[str] = "And how long will it take?"
    QUEUED_ANSWER: ClassVar[str] = "About an hour on the stand."
    LATER: ClassVar[str] = "Then check the indexes"
    DRAFT: ClassVar[str] = "a draft typed after the stop"
    HELD_SEC: ClassVar[float] = 60.0
    TURN_SEC: ClassVar[float] = 120.0
    QUEUED_LOG: ClassVar[str] = "goes to the queue of session"
    RESTORE: ClassVar[str] = "boba:composer-restore"
    STOP: ClassVar[str] = "#stop-button"


@pytest.fixture
def queue_book(llm_port: int) -> ReplyBookClient:
    book = ReplyBookClient(StandUrl.of(llm_port))
    book.register(
        [
            Reply(
                question=QueueBook.HELD,
                turns=[
                    ReplyTurn(
                        content=QueueBook.HELD_ANSWER,
                        hold=True,
                        hold_after_tokens=QueueBook.HELD_TOKENS,
                    )
                ],
            ),
            Reply(
                question=QueueBook.QUEUED,
                turns=[ReplyTurn(content=QueueBook.QUEUED_ANSWER)],
            ),
            Reply(
                question=QueueBook.LATER,
                turns=[ReplyTurn(content=QueueBook.QUEUED_ANSWER)],
            ),
        ]
    )
    return book


class QueuedFeed:
    """Поле ввода и лента глазами теста очереди."""

    def __init__(self, chat: ChatPage, stand: StandProcess) -> None:
        self._chat = chat
        self._stand = stand
        self._page = chat.page

    @property
    def field(self) -> Locator:
        return self._page.locator(Selector.INPUT.value)

    @property
    def send(self) -> Locator:
        return self._page.locator(Selector.SUBMIT.value)

    @property
    def stop(self) -> Locator:
        return self._page.locator(QueueBook.STOP)

    def type_and_enter(self, text: str) -> None:
        self.field.fill(text)
        self.field.press("Enter")
        expect(self.field).to_have_value("")

    def questions(self) -> list[str]:
        """Тексты вопросов ленты по порядку."""
        found: list[str] = []
        for step in self._page.locator(Selector.of_type(StepKind.USER.value)).all():
            found.append(step.inner_text().strip())

        return found

    def answers(self) -> list[str]:
        found: list[str] = []
        steps = self._page.locator(Selector.of_type(StepKind.ASSISTANT.value))
        for step in steps.all():
            found.append(step.inner_text().strip())

        return found

    def await_queued(self, count: int) -> None:
        """Сервер поставил count вопросов в очередь сессии агента."""
        deadline = time.monotonic() + QueueBook.HELD_SEC
        while time.monotonic() < deadline:
            if self.queued_so_far() >= count:
                return

            time.sleep(0.1)

        msg = (
            f"{count} queued question(s) did not reach the agent: "
            f"{self._stand.tail(40)}"
        )
        raise AssertionError(msg)

    def queued_so_far(self) -> int:
        """Строки лога стенда о вопросах, поставленных в очередь."""
        marker = QueueBook.QUEUED_LOG.encode()
        found = 0
        with self._stand.log_path.open("rb") as handle:
            for line in handle:
                if marker in line:
                    found += 1

        return found

    def reload(self) -> None:
        """Тот же тред, открытый заново: лента собирается из истории агента."""
        thread_id = self._chat.log.thread_id()
        self._page.goto(
            f"{self._chat.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        self.field.wait_for(timeout=60_000)
        self._page.wait_for_load_state("networkidle")


class TestQueuedInput:
    def test_stop_and_send_swap_with_the_field_during_a_turn(
        self, chat: ChatPage, stand: StandProcess, queue_book: ReplyBookClient
    ) -> None:
        feed = QueuedFeed(chat, stand)
        chat.ask(QueueBook.HELD)
        queue_book.await_held(QueueBook.HELD, QueueBook.HELD_SEC)

        expect(feed.stop).to_be_visible()
        expect(feed.send).to_have_count(0)
        expect(feed.field).to_be_editable()

        feed.field.fill(QueueBook.QUEUED)
        expect(feed.send).to_be_visible()
        expect(feed.send).to_be_enabled()
        expect(feed.stop).to_have_count(0)

        feed.field.fill("")
        expect(feed.stop).to_be_visible()
        expect(feed.send).to_have_count(0)

        feed.stop.click()
        chat.await_idle(QueueBook.TURN_SEC)
        queue_book.release(QueueBook.HELD)
        expect(feed.send).to_be_visible()
        expect(feed.stop).to_have_count(0)

    def test_a_message_sent_during_the_turn_is_answered_after_it(
        self, chat: ChatPage, stand: StandProcess, queue_book: ReplyBookClient
    ) -> None:
        feed = QueuedFeed(chat, stand)
        chat.ask(QueueBook.HELD)
        queue_book.await_held(QueueBook.HELD, QueueBook.HELD_SEC)
        before = feed.queued_so_far()

        feed.type_and_enter(QueueBook.QUEUED)
        feed.await_queued(before + 1)
        expect(
            chat.page.locator(
                Selector.of_type(StepKind.USER.value), has_text=QueueBook.QUEUED
            )
        ).to_be_visible()
        expect(feed.stop).to_be_visible()
        assert QueueBook.HELD in queue_book.held()

        queue_book.release(QueueBook.HELD)
        chat.await_idle(QueueBook.TURN_SEC)
        expect(chat.page.get_by_text(QueueBook.QUEUED_ANSWER)).to_be_visible(
            timeout=60_000
        )

        live_questions = feed.questions()
        live_answers = feed.answers()
        assert len(live_questions) == 2, live_questions
        assert QueueBook.HELD in live_questions[0], live_questions
        assert QueueBook.QUEUED in live_questions[1], live_questions
        assert QueueBook.HELD_ANSWER in live_answers[0], live_answers
        assert QueueBook.QUEUED_ANSWER in live_answers[-1], live_answers

        feed.reload()
        expect(chat.page.get_by_text(QueueBook.QUEUED_ANSWER)).to_be_visible(
            timeout=60_000
        )
        assert feed.questions() == live_questions
        assert feed.answers() == live_answers

    def test_stop_returns_queued_messages_to_the_field(
        self, chat: ChatPage, stand: StandProcess, queue_book: ReplyBookClient
    ) -> None:
        feed = QueuedFeed(chat, stand)
        chat.ask(QueueBook.HELD)
        queue_book.await_held(QueueBook.HELD, QueueBook.HELD_SEC)
        before = feed.queued_so_far()

        feed.type_and_enter(QueueBook.QUEUED)
        feed.type_and_enter(QueueBook.LATER)
        feed.await_queued(before + 2)

        feed.stop.click()
        chat.await_idle(QueueBook.TURN_SEC)
        queue_book.release(QueueBook.HELD)

        restored = f"{QueueBook.QUEUED}\n\n{QueueBook.LATER}"
        expect(feed.field).to_have_value(restored, timeout=30_000)
        expect(feed.send).to_be_visible()
        questions = feed.questions()
        assert len(questions) == 1, questions
        assert QueueBook.HELD in questions[0], questions

        feed.field.fill(QueueBook.DRAFT)
        chat.page.evaluate(
            "([type, text]) => window.postMessage({type, texts: [text]}, '*')",
            [QueueBook.RESTORE, QueueBook.LATER],
        )
        expect(feed.field).to_have_value(f"{QueueBook.DRAFT}\n\n{QueueBook.LATER}")

        feed.reload()
        asked = chat.page.locator(
            Selector.of_type(StepKind.USER.value), has_text=QueueBook.HELD
        )
        expect(asked).to_have_count(1, timeout=60_000)
        assert feed.questions() == questions
