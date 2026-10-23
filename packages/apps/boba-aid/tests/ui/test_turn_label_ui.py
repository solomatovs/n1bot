"""Подпись свёрнутого контейнера хода: пока ход идёт, контейнер подписан
текущим действием хода и мерцает, как идущий шаг; по концу хода — итоговая
подпись без мерцания.

Ход — книга ответов FakeLLM: рассуждение, вызов инструмента с длинной
подписью и ответ, чей поток стоит на паузе до первого токена, пока тест
смотрит на свёрнутый контейнер. Последовательность подписей берётся из
socket.io (кадры шага run), вид — из DOM.
"""

from __future__ import annotations

import re
from typing import ClassVar

import pytest
from playwright.sync_api import Locator, expect

from boba.aid.chat.ui_text import UiText
from boba.aid.rendering.chat_view import StepElapsed, StepText
from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import Reply, ReplyBookClient, ReplyCall, ReplyTurn
from boba.stand.ui.stand import StandUrl

pytestmark = pytest.mark.ui


class LabelBook:
    """Вопрос и ход книги этого модуля."""

    QUESTION: ClassVar[str] = "Какие подключения мне доступны сейчас?"
    TOOL: ClassVar[str] = "connection_list"
    INTENT: ClassVar[str] = (
        "Смотрю все подключения пользователя по профилям и серверам, чтобы "
        "перечислить базы, хранилища и сервисы с их адресами и сроками билетов"
    )
    """Подпись короче предела подписи вызова (ToolIntent.MAX_CHARS), но
    длиннее строки контейнера."""
    REASONING: ClassVar[str] = "Сначала посмотрю список подключений."
    LOOKING: ClassVar[str] = "Смотрю подключения."
    ANSWER: ClassVar[str] = "Подключений пока нет, их можно добавить в настройках."
    SHIMMER: ClassVar[re.Pattern[str]] = re.compile(r"\bloading-shimmer\b")
    HELD_SEC: ClassVar[float] = 60.0

    def replies(self) -> list[Reply]:
        call = ReplyCall(tool=self.TOOL, arguments={}, intent=self.INTENT)
        return [
            Reply(
                question=self.QUESTION,
                turns=[
                    ReplyTurn(
                        reasoning=self.REASONING, content=self.LOOKING, calls=[call]
                    ),
                    ReplyTurn(content=self.ANSWER, hold=True),
                ],
            )
        ]


@pytest.fixture
def label_book(llm_port: int) -> ReplyBookClient:
    client = ReplyBookClient(StandUrl.of(llm_port))
    client.register(LabelBook().replies())
    return client


def _labels(chat: ChatPage) -> list[str]:
    """Подписи контейнера хода по кадрам сокета, без повторов подряд."""
    names: list[str] = []
    for step in chat.log.steps_of_type(StepKind.RUN.value):
        name = str(step.get("name", ""))
        if names and names[-1] == name:
            continue

        names.append(name)

    return names


def _toggle(chat: ChatPage) -> Locator:
    """Заголовок контейнера хода: по нему контейнер раскрывают."""
    container = chat.steps_of(StepKind.RUN.value).last
    return container.locator(Selector.STEP_TOGGLE.value).first


def test_collapsed_container_shows_the_current_action(
    chat: ChatPage, label_book: ReplyBookClient, app_texts: UiText
) -> None:
    thinking = app_texts.text(StepText.THINKING.value)
    called = f"{LabelBook.TOOL} · {LabelBook.INTENT}"
    chat.ask(LabelBook.QUESTION)
    label_book.await_held(LabelBook.QUESTION, LabelBook.HELD_SEC)

    toggle = _toggle(chat)
    expect(toggle).to_contain_text(f"✔ {called}", timeout=30_000)
    expect(toggle).to_have_class(LabelBook.SHIMMER)
    expect(toggle).to_have_attribute(Selector.TOGGLE_STATE.value, "closed")

    # длинная подпись обрезается по ширине строки, а не переносится
    label = toggle.locator("span[title]")
    expect(label).to_have_attribute("title", re.compile(re.escape(called)))
    clipped = label.evaluate(
        "(node) => [node.scrollWidth, node.clientWidth, "
        "getComputedStyle(node).textOverflow, getComputedStyle(node).whiteSpace]"
    )
    if clipped[0] <= clipped[1]:
        raise AssertionError(f"the long label is not clipped: {clipped}")

    if clipped[2:] != ["ellipsis", "nowrap"]:
        raise AssertionError(
            f"the long label is not one line with an ellipsis: {clipped}"
        )

    widths = chat.page.evaluate(
        "() => [document.documentElement.scrollWidth, "
        "document.documentElement.clientWidth]"
    )
    if widths[0] > widths[1]:
        raise AssertionError(f"the feed scrolls sideways: {widths}")

    label_book.release(LabelBook.QUESTION)
    chat.await_idle()

    done = app_texts.text(StepText.DONE_STEPS.value, count=2)
    expect(toggle).to_have_text(done)
    expect(toggle).not_to_have_class(LabelBook.SHIMMER)

    labels = _labels(chat)
    elapsed = StepElapsed(app_texts).titled(f"✔ {called}", False)
    expected = [f"○ {thinking}", f"✔ {thinking}", f"○ {called}"]
    if labels[:3] != expected:
        raise AssertionError(f"container labels {labels}, expected to start {expected}")

    if not re.match(elapsed, labels[3]):
        raise AssertionError(f"the finished call is not the label: {labels}")

    if labels[4:] != [done]:
        raise AssertionError(f"the turn does not end with {done!r}: {labels}")
