"""Висящий вопрос агента во вкладке (QuestionAsk) на настоящих сессиях
chainlit: элемент и `ask` уходят вкладке, ответ панели разбирается в ответ
формы, срок и снятие вопроса дают «нет ответа», переподключённая вкладка
получает вопрос заново, а прежняя убирает панель. Каждое ожидание кончается
снятием панели и элемента и возвратом индикатора хода.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from chainlit.element import CustomElement
from chainlit.session import WebsocketSession
from chainlit.step import StepDict
from chainlit.user import PersistedUser
from pydantic import ValidationError

from boba.aid.agent.feed import AskEvent, QuestionAsk, TabSurface

pytestmark = pytest.mark.anyio

THREAD = "thread-question-1"
STEP = "step-question-1"
USER = PersistedUser(id="u-1", identifier="tester", createdAt="", metadata={})


class Tab:
    """Вкладка теста: что ушло в её сокет и как она отвечает на `ask`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[tuple[str, Any]] = []
        self.asked = asyncio.Event()
        self.reply: Mapping[str, Any] | None = None
        self.answer = asyncio.Event()
        self.session = WebsocketSession(
            id=f"session-{name}",
            socket_id=f"socket-{name}",
            emit=self.emit,
            emit_call=self.emit_call,
            user_env={},
            client_type="webapp",
            thread_id=THREAD,
            user=USER,
        )

    def emit(self, event: str, data: Any) -> Any:
        self.events.append((event, data))

        return self._sent()

    async def _sent(self) -> None:
        return None

    async def emit_call(self, event: str, data: Any, timeout: int | None) -> Any:
        self.events.append((event, data))
        self.asked.set()
        await self.answer.wait()

        return self.reply

    def names(self) -> list[str]:
        found: list[str] = []
        for event, _ in self.events:
            found.append(event)

        return found


@pytest.fixture
async def tabs() -> AsyncIterator[tuple[Tab, Tab]]:
    first = Tab("first")
    second = Tab("second")
    try:
        yield first, second
    finally:
        await first.session.delete()
        await second.session.delete()


def _ask(tab: Tab, timeout_sec: float) -> QuestionAsk:
    element = CustomElement(
        name="Question", props={"state": "pending"}, display="inline"
    )
    element.for_id = STEP
    step = StepDict(id=STEP, parentId=None, threadId=THREAD)

    return QuestionAsk(element, step, STEP, TabSurface(tab.session), timeout_sec)


async def test_answer_of_the_panel_is_the_reply(tabs: tuple[Tab, Tab]) -> None:
    first, _ = tabs
    first.reply = {"action": "accept", "content": {"q1": "A"}, "submitted": True}
    first.answer.set()

    reply = await _ask(first, 5.0).reply()

    assert reply is not None
    assert reply.response().model_dump(by_alias=True, exclude_none=True) == {
        "action": "accept",
        "content": {"q1": "A"},
    }
    assert first.names() == [
        "element",
        AskEvent.ASK.value,
        AskEvent.CLEAR.value,
        AskEvent.REMOVE_ELEMENT.value,
        "task_start",
    ]
    ask = first.events[1][1]
    assert ask["spec"]["type"] == "element"
    assert ask["spec"]["step_id"] == STEP
    assert ask["spec"]["element_id"] == first.events[0][1]["id"]


async def test_deadline_cancels_and_clears_the_panel(tabs: tuple[Tab, Tab]) -> None:
    first, _ = tabs

    reply = await _ask(first, 0.3).reply()

    assert reply is None
    assert first.names()[-3:] == [
        AskEvent.CLEAR.value,
        AskEvent.REMOVE_ELEMENT.value,
        "task_start",
    ]


async def test_reconnected_tab_gets_the_question_again(tabs: tuple[Tab, Tab]) -> None:
    """Вопрос переезжает во вкладку, подключившуюся заново: прежняя убирает
    панель, ответ приходит из новой."""
    first, second = tabs
    ask = _ask(first, 5.0)
    waiting = asyncio.ensure_future(ask.reply())
    await asyncio.wait_for(first.asked.wait(), 5.0)

    second.reply = {"action": "decline", "submitted": True}
    second.answer.set()
    ask.show_in(TabSurface(second.session))
    reply = await asyncio.wait_for(waiting, 5.0)

    assert reply is not None
    assert reply.action == "decline"
    assert ask.tab_id == second.session.id
    assert first.names()[-3:] == [
        AskEvent.CLEAR.value,
        AskEvent.REMOVE_ELEMENT.value,
        "task_start",
    ]
    assert second.names()[:2] == ["element", AskEvent.ASK.value]


async def test_stopped_turn_cancels_the_question(tabs: tuple[Tab, Tab]) -> None:
    first, _ = tabs
    ask = _ask(first, 5.0)
    waiting = asyncio.ensure_future(ask.reply())
    await asyncio.wait_for(first.asked.wait(), 5.0)

    ask.stop()

    assert await asyncio.wait_for(waiting, 5.0) is None
    assert AskEvent.CLEAR.value in first.names()


async def test_answer_off_the_form_is_an_error(tabs: tuple[Tab, Tab]) -> None:
    first, _ = tabs
    first.reply = {"action": "maybe", "submitted": True}
    first.answer.set()

    with pytest.raises(ValidationError):
        await _ask(first, 5.0).reply()
