"""Лента чата: раскладка шагов хода."""

from __future__ import annotations

import pytest

from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.chat_view import (
    ChatView,
    RecordingSink,
    StepRole,
    TurnPulse,
)
from chainlit.step import StepDict

THREAD = "11111111-1111-1111-1111-111111111111"
TURN = "22222222-2222-2222-2222-222222222222"
PULSE = ChatView.derive_id(THREAD, TURN, StepRole.PULSE)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: БД этим тестам не нужна."""


@pytest.fixture
async def http_context() -> None:
    """Message и Step пишут в emitter сессии."""
    from chainlit.context import init_http_context

    init_http_context()


class TestAnswerOrder:
    """Live обязан давать тот же порядок, что сборка истории из checkpointer."""

    @pytest.mark.anyio
    async def test_tool_seals_the_current_answer(self, http_context: None) -> None:
        """Текст после инструмента — новое сообщение, иначе элемент тула уедет вниз."""
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        await view.stream_answer("Сейчас нарисую", TURN)
        first = view.answer_message
        if first is None:
            raise AssertionError("first is not None")

        await view.tool_started("diagram_save", {"name": "a.mmd"}, "call-1")

        if view.answer_message is not None:
            raise AssertionError("view.answer_message is None")

        await view.stream_answer("Готово", TURN)
        second = view.answer_message

        if second is None:
            raise AssertionError("second is not None")
        if second.id == first.id:
            raise AssertionError("second.id != first.id")

    @pytest.mark.anyio
    async def test_answers_of_one_turn_have_distinct_ids(
        self, http_context: None
    ) -> None:
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        seen: list[str] = []
        for index in range(3):
            await view.stream_answer(f"часть {index}", TURN)
            message = view.answer_message
            if message is None:
                raise AssertionError("message is not None")
            seen.append(message.id)
            await view.tool_started("bash", {"cmd": "ls"}, f"call-{index}")

        if len(set(seen)) != len(seen):
            raise AssertionError("len(set(seen)) == len(seen)")


class TestTurnPulse:
    """Кружок ожидания живёт весь ход и всегда стоит последним в ленте."""

    @staticmethod
    def _roots(steps: list[StepDict]) -> list[str]:
        """id корневых шагов ленты: вложенные её конца не двигают."""
        ids: list[str] = []
        for step in steps:
            if step.get(StepField.PARENT_ID):
                continue

            ids.append(str(step.get(StepField.ID, "")))

        return ids

    @staticmethod
    def _pulse(steps: list[StepDict]) -> StepDict | None:
        for step in steps:
            if step.get(StepField.ID) == PULSE:
                return step

        return None

    @pytest.mark.anyio
    async def test_pulse_opens_the_turn(self, http_context: None) -> None:
        """До первого токена ход виден кружком, а не пустой лентой."""
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="Пользователь")
        view.begin_turn(TURN)

        await view.await_model()

        drawn = self._pulse(sink.steps)
        if drawn is None:
            raise AssertionError("drawn is not None")
        if drawn.get(StepField.OUTPUT) != TurnPulse.CONTENT:
            raise AssertionError("drawn output is the pulse content")
        if self._roots(sink.steps)[-1] != PULSE:
            raise AssertionError("pulse is the last step")

    @pytest.mark.anyio
    async def test_answer_stream_holds_the_pulse(self, http_context: None) -> None:
        """Стримящийся ответ рисует курсор сам: второго кружка быть не должно."""
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="Пользователь")
        view.begin_turn(TURN)

        await view.await_model()
        await view.stream_answer("текст", TURN)

        if self._pulse(sink.steps) is not None:
            raise AssertionError("pulse is hidden while the answer streams")

    @pytest.mark.anyio
    async def test_tool_call_keeps_the_pulse_last(self, http_context: None) -> None:
        """Вызов инструмента закрывает ответ — кружок возвращается под него."""
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="Пользователь")
        view.begin_turn(TURN)

        await view.await_model()
        await view.stream_answer("сейчас посмотрю", TURN)
        await view.tool_started("bash", {"cmd": "ls"}, "call-1")

        if self._pulse(sink.steps) is None:
            raise AssertionError("pulse is back while the tool runs")
        if self._roots(sink.steps)[-1] != PULSE:
            raise AssertionError("pulse is the last step")

    @pytest.mark.anyio
    async def test_finished_turn_has_no_pulse(self, http_context: None) -> None:
        """Ход закончился — мигать больше нечему."""
        sink = RecordingSink()
        view = ChatView(THREAD, sink, user_name="Пользователь")
        view.begin_turn(TURN)

        await view.await_model()
        await view.tool_started("bash", {"cmd": "ls"}, "call-1")
        await view.finish_turn()

        if self._pulse(sink.steps) is not None:
            raise AssertionError("finished turn has no pulse")
