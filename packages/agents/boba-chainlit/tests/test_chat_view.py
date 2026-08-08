"""Лента чата: раскладка шагов хода и рендер входа инструментов."""

from __future__ import annotations

from typing import Any

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
        assert first is not None

        await view.tool_started("diagram_save", {"name": "a.mmd"}, "call-1")

        assert view.answer_message is None

        await view.stream_answer("Готово", TURN)
        second = view.answer_message

        assert second is not None
        assert second.id != first.id

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
            assert message is not None
            seen.append(message.id)
            await view.tool_started("bash", {"cmd": "ls"}, f"call-{index}")

        assert len(set(seen)) == len(seen)


class TestToolInput:
    """Вход инструмента читается человеком: спека и код — не json со \\n."""

    @staticmethod
    def _render(args: dict[str, Any]) -> tuple[str, str | bool]:
        return ChatView._render_args(args)

    def test_multiline_argument_becomes_markdown(self) -> None:
        rendered, show_input = self._render(
            {"name": "a.mmd", "spec": "flowchart LR\n    A --> B"}
        )

        assert show_input is True
        assert "**spec:**" in rendered
        assert "```\nflowchart LR\n    A --> B\n```" in rendered
        assert "\\n" not in rendered

    def test_single_line_arguments_stay_json(self) -> None:
        rendered, show_input = self._render({"path": "/workspace/a.png"})

        assert show_input == "json"
        assert rendered.startswith("{")

    def test_fence_longer_than_any_inside_the_value(self) -> None:
        """Спека с ``` внутри не должна разрывать блок."""
        rendered, _ = self._render({"spec": "flowchart LR\n```\n    A --> B"})

        assert "````\nflowchart LR" in rendered
