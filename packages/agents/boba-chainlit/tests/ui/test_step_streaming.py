"""Потоковость шагов ленты: каждый шаг приходит по токену, а не целиком.

Доказательство берётся из socket.io: у стримящегося шага сначала stream_start,
затем stream_token, и только потом финальная отправка. Отрисовка проверяется в
DOM: шаг присутствует в дереве под своим типом.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.chainlit.rendering.chat_view import StepText
from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.socket_log import ChatEvent, SocketLog

pytestmark = pytest.mark.ui

MIN_TOKENS = 2


def _assert_streamed(log: SocketLog, step_type: str) -> str:
    """Шаг такого типа стримился: старт, токены и лишь затем финальная отправка."""
    started = log.streamed_steps(step_type)
    if not (started):
        raise AssertionError(f"no stream_start for {step_type}\n{log.describe()}")

    step_id = started[0]
    tokens = log.tokens_of(step_id)
    if len(tokens) < MIN_TOKENS:
        raise AssertionError(
            f"{step_type} got {len(tokens)} tokens, expected at least {MIN_TOKENS}"
            f"\n{log.describe()}"
        )

    start_at = log.index_of(ChatEvent.STREAM_START, step_id)
    final_at = log.index_of(ChatEvent.NEW_MESSAGE, step_id)
    if final_at <= start_at:
        raise AssertionError(
            f"{step_type} was sent whole before streaming\n{log.describe()}"
        )

    last_token_at = -1
    for index, frame in enumerate(log.frames):
        if frame.event is not ChatEvent.STREAM_CHUNK:
            continue

        if frame.step_id != step_id:
            continue

        last_token_at = index

    if last_token_at >= final_at:
        raise AssertionError(
            f"{step_type} finished before its last token\n{log.describe()}"
        )
    return step_id


class TestThinkingStep:
    """Рассуждения модели: шаг llm под контейнером процесса."""

    def test_streams_token_by_token(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.THINKING_ANSWER.value} please")
        chat.await_idle()

        _assert_streamed(chat.log, StepKind.LLM.value)

    def test_is_shown_in_dom(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.THINKING_ANSWER.value} please")
        chat.await_idle()
        chat.expand_process()

        step = chat.expand_step(StepKind.LLM.value)
        if "reason" not in step.inner_text().lower():
            raise AssertionError(step.inner_text())


class TestAnswerStep:
    """Ответ ассистента: сообщение ленты верхнего уровня."""

    def test_streams_token_by_token(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.ANSWER.value} please")
        chat.await_idle()

        _assert_streamed(chat.log, StepKind.ASSISTANT.value)

    def test_is_shown_in_dom(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.ANSWER.value} please")
        chat.await_idle()

        step = chat.await_step(StepKind.ASSISTANT.value)
        if "streamed answer" not in step.inner_text():
            raise AssertionError('"streamed answer" in step.inner_text()')


class TestToolStep:
    """Инструмент: шаг появляется работающим и потом дополняется результатом."""

    def test_appears_before_its_result(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        steps = chat.log.steps_of_type(StepKind.TOOL.value)
        if not (steps):
            raise AssertionError(chat.log.describe())

        step_id = str(steps[0]["id"])
        appeared_at = chat.log.index_of(ChatEvent.NEW_MESSAGE, step_id)
        updated_at = chat.log.index_of(ChatEvent.UPDATE_MESSAGE, step_id)
        if appeared_at < 0:
            raise AssertionError(chat.log.describe())
        if updated_at <= appeared_at:
            raise AssertionError(
                f"tool step was not updated after it appeared\n{chat.log.describe()}"
            )

    def test_running_state_precedes_the_result(self, chat: ChatPage) -> None:
        """Первым приходит открытый шаг с подписью вызова, а не готовый вывод.

        Состояние «идёт» показывает открытый шаг (кружок ○, нет end, маркер
        running вместо вывода); результат инструмента приходит только потом.
        """
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        steps = chat.log.steps_of_type(StepKind.TOOL.value)
        if len(steps) < 2:
            raise AssertionError(chat.log.describe())

        first = steps[0]
        if not str(first.get("name", "")).startswith("○"):
            raise AssertionError(first)
        if first.get("end") is not None:
            raise AssertionError(first)

        # секцию output фронт рисует только непустой: под ней живёт кнопка
        # живого вывода, поэтому идущий шаг несёт маркер, а не результат
        if first.get("output") != StepText.RUNNING.value:
            raise AssertionError(first)

        last = steps[-1]
        if last.get("end") is None:
            raise AssertionError(last)
        if not last.get("output"):
            raise AssertionError(last)

    def test_is_shown_in_dom(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()
        chat.expand_process()

        step = chat.expand_step(StepKind.TOOL.value)
        if "connection_list" not in step.inner_text():
            raise AssertionError(step.inner_text())


class TestTurnOrder:
    """Порядок ленты: рассуждения раньше инструмента, инструмент раньше ответа."""

    def test_thinking_precedes_tool_and_answer(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        thinking = chat.log.streamed_steps(StepKind.LLM.value)
        if not (thinking):
            raise AssertionError(chat.log.describe())

        tools = chat.log.steps_of_type(StepKind.TOOL.value)
        if not (tools):
            raise AssertionError(chat.log.describe())

        thinking_at = chat.log.index_of(ChatEvent.STREAM_START, thinking[0])
        tool_at = chat.log.index_of(ChatEvent.NEW_MESSAGE, str(tools[0]["id"]))
        answers = chat.log.streamed_steps(StepKind.ASSISTANT.value)
        if not (answers):
            raise AssertionError(chat.log.describe())

        answer_at = chat.log.index_of(ChatEvent.STREAM_START, answers[0])
        if not (thinking_at < tool_at < answer_at):
            raise AssertionError(chat.log.describe())


class TestEditedQuestion:
    """Правка вопроса: ход после правки показывается целиком, как первый.

    Вкладки собирают ленту заново из истории, а контейнер и ответ нового хода
    получают те же id, что у прежнего: лента обязана прислать их новыми
    шагами, а не обновлениями к тому, чего во вкладке уже нет. DOM после
    правки — тот же полный ход: вопрос, process с рассуждением и
    инструментом внутри, ответ.
    """

    CALL: ClassVar[str] = '{"name": "connection_list", "arguments": {}}'
    EDITED_CALL: ClassVar[str] = (
        '{"name": "connection_list", "arguments": {}, "edit": 1}'
    )

    @staticmethod
    def _outline(chat: ChatPage) -> list[str]:
        """Типы шагов в порядке DOM; process раскрыт, чтобы дети были в дереве."""
        chat.expand_process()
        types: list[str] = []
        for node in chat.page.locator(Selector.STEP.value).all():
            types.append(str(node.get_attribute("data-step-type")))

        return types

    def test_dom_after_the_edit_is_a_full_turn(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.CALL.value} {self.CALL}")
        chat.await_idle()
        before = self._outline(chat)
        expected = [
            StepKind.USER.value,
            StepKind.RUN.value,
            StepKind.LLM.value,
            StepKind.TOOL.value,
            StepKind.ASSISTANT.value,
        ]
        if before != expected:
            raise AssertionError(f"first turn outline: {before}")

        page = chat.page
        page.locator(".edit-message").last.click(force=True)
        page.locator("#edit-chat-input").fill(
            f"{ScenarioName.CALL.value} {self.EDITED_CALL}"
        )
        chat.log.clear()
        page.locator(".confirm-edit").click()
        chat.await_idle()

        runs = chat.log.steps_of_type(StepKind.RUN.value)
        if not runs:
            raise AssertionError(
                f"no process step after the edit\n{chat.log.describe()}"
            )

        run_id = str(runs[0]["id"])
        if chat.log.index_of(ChatEvent.NEW_MESSAGE, run_id) < 0:
            raise AssertionError(
                f"the process step came as an update, not a new step\n"
                f"{chat.log.describe()}"
            )

        after = self._outline(chat)
        if after != expected:
            raise AssertionError(
                f"outline after the edit: {after}, expected {expected}"
            )

        step = chat.expand_step(StepKind.TOOL.value)
        if "connection_list" not in step.inner_text():
            raise AssertionError(step.inner_text())
