"""Лента чата: раскладка шагов хода."""

from __future__ import annotations

import pytest

from boba.chainlit.rendering.chat_view import ChatView, RecordingSink

THREAD = "11111111-1111-1111-1111-111111111111"
TURN = "22222222-2222-2222-2222-222222222222"


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
