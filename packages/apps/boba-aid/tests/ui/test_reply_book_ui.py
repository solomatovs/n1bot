"""Книга ответов FakeLLM в настоящем ходе чата: вопрос в пузыре и имя треда —
обычный текст, ответ — из книги, поток стоит ровно после заданных токенов до
release; формулы KaTeX рисуются, а «$» у денег остаётся текстом.

Ключ книги — набранный пользователем текст: агент кладёт в то же сообщение
свои служебные блоки, и что дошло до модели, тест сверяет по журналу
/requests стенда.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest
from playwright.sync_api import Locator, expect

from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import (
    FakeRoute,
    Reply,
    ReplyBookClient,
    ReplyQuestions,
    ReplyTurn,
)
from boba.stand.ui.stand import StandUrl

pytestmark = pytest.mark.ui


class BookQuestion:
    """Вопросы книги этого модуля и ответы на них."""

    STATUS: ClassVar[str] = "Какой сегодня статус выгрузки?"
    STATUS_ANSWER: ClassVar[str] = "Выгрузка закончилась в 06:10, ошибок нет."
    STEPS: ClassVar[str] = "Опиши шаги миграции"
    STEPS_ANSWER: ClassVar[str] = (
        "Снять копию, перенести схему, перелить данные и сверить счётчики."
    )
    STEPS_HELD: ClassVar[int] = 4
    MARGIN: ClassVar[str] = "Как считается маржа?"
    MARGIN_ANSWER: ClassVar[str] = (
        "Маржа $m = (p - c) / p$, а цены $5 и $10 остаются текстом.\n\n$$\\sum_i m_i$$"
    )
    MONEY: ClassVar[str] = "цены $5 и $10 остаются текстом"


@pytest.fixture
def book(llm_port: int) -> ReplyBookClient:
    client = ReplyBookClient(StandUrl.of(llm_port))
    client.register(
        [
            Reply(
                question=BookQuestion.STATUS,
                turns=[ReplyTurn(content=BookQuestion.STATUS_ANSWER)],
            ),
            Reply(
                question=BookQuestion.STEPS,
                turns=[
                    ReplyTurn(
                        content=BookQuestion.STEPS_ANSWER,
                        hold=True,
                        hold_after_tokens=BookQuestion.STEPS_HELD,
                    )
                ],
            ),
            Reply(
                question=BookQuestion.MARGIN,
                turns=[ReplyTurn(content=BookQuestion.MARGIN_ANSWER)],
            ),
        ]
    )
    return client


class BookFeed:
    """Лента и журнал модели глазами теста книги ответов."""

    def __init__(self, chat: ChatPage, llm_port: int) -> None:
        self._chat = chat
        self._llm_port = llm_port

    def answer(self, text: str) -> Locator:
        return self._chat.steps_of(StepKind.ASSISTANT.value).filter(has_text=text).last

    def questions(self) -> list[str]:
        """Вопросы, которые модель увидела в запросах этого теста."""
        url = StandUrl.of(self._llm_port, FakeRoute.REQUESTS.value)
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        payloads: list[dict[str, Any]] = response.json()["requests"]

        found: list[str] = []
        for payload in payloads:
            found.append(ReplyQuestions().of(payload))

        return found


def test_plain_question_is_answered_from_the_book(
    chat: ChatPage, book: ReplyBookClient, llm_port: int
) -> None:
    chat.ask(BookQuestion.STATUS)
    chat.await_idle()

    feed = BookFeed(chat, llm_port)
    expect(feed.answer(BookQuestion.STATUS_ANSWER)).to_be_visible()
    question = chat.steps_of(StepKind.USER.value).last
    expect(question).to_contain_text(BookQuestion.STATUS)
    expect(chat.page.locator("#thread-history")).to_contain_text(
        BookQuestion.STATUS, timeout=30_000
    )

    questions = feed.questions()
    if questions != [BookQuestion.STATUS]:
        raise AssertionError(f"questions seen by the model: {questions}")


def test_held_stream_shows_the_given_tokens_until_release(
    chat: ChatPage, book: ReplyBookClient, llm_port: int
) -> None:
    feed = BookFeed(chat, llm_port)
    chat.ask(BookQuestion.STEPS)
    book.await_held(BookQuestion.STEPS, 60.0)

    words = BookQuestion.STEPS_ANSWER.split(" ")
    shown = " ".join(words[: BookQuestion.STEPS_HELD])
    held = feed.answer(shown)
    expect(held).to_be_visible(timeout=30_000)
    chat.page.wait_for_timeout(500)
    if words[BookQuestion.STEPS_HELD] in held.inner_text():
        raise AssertionError(f"the held stream went on: {held.inner_text()!r}")

    book.release(BookQuestion.STEPS)
    chat.await_idle()

    expect(feed.answer(BookQuestion.STEPS_ANSWER)).to_be_visible()


def test_formulas_render_and_money_stays_text(
    chat: ChatPage, book: ReplyBookClient, llm_port: int
) -> None:
    feed = BookFeed(chat, llm_port)
    chat.ask(BookQuestion.MARGIN)
    chat.await_idle()

    answer = feed.answer(BookQuestion.MONEY)
    expect(answer).to_be_visible()
    formulas = answer.locator(".katex")
    if formulas.count() != 2:
        raise AssertionError(f"inline and block formula, got {formulas.count()}")
