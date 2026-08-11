"""Потоковость шагов ленты: каждый шаг приходит по токену, а не целиком.

Доказательство берётся из socket.io: у стримящегося шага сначала stream_start,
затем stream_token, и только потом финальная отправка. Отрисовка проверяется в
DOM: шаг присутствует в дереве под своим типом.
"""

from __future__ import annotations

from ui.chat_page import ChatPage, StepKind
from ui.fake_llm import ScenarioName
from ui.socket_log import ChatEvent, SocketLog

MIN_TOKENS = 2


def _assert_streamed(log: SocketLog, step_type: str) -> str:
    """Шаг такого типа стримился: старт, токены и лишь затем финальная отправка."""
    started = log.streamed_steps(step_type)
    assert started, f"no stream_start for {step_type}\n{log.describe()}"

    step_id = started[0]
    tokens = log.tokens_of(step_id)
    assert len(tokens) >= MIN_TOKENS, (
        f"{step_type} got {len(tokens)} tokens, expected at least {MIN_TOKENS}"
        f"\n{log.describe()}"
    )

    start_at = log.index_of(ChatEvent.STREAM_START, step_id)
    final_at = log.index_of(ChatEvent.NEW_MESSAGE, step_id)
    assert final_at > start_at, (
        f"{step_type} was sent whole before streaming\n{log.describe()}"
    )

    last_token_at = -1
    for index, frame in enumerate(log.frames):
        if frame.event is not ChatEvent.STREAM_TOKEN:
            continue

        if frame.step_id != step_id:
            continue

        last_token_at = index

    assert last_token_at < final_at, (
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
        assert "reason" in step.inner_text().lower(), step.inner_text()


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
        assert "streamed answer" in step.inner_text()


class TestToolStep:
    """Инструмент: шаг появляется работающим и потом дополняется результатом."""

    def test_appears_before_its_result(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        steps = chat.log.steps_of_type(StepKind.TOOL.value)
        assert steps, chat.log.describe()

        step_id = str(steps[0]["id"])
        appeared_at = chat.log.index_of(ChatEvent.NEW_MESSAGE, step_id)
        updated_at = chat.log.index_of(ChatEvent.UPDATE_MESSAGE, step_id)
        assert appeared_at >= 0, chat.log.describe()
        assert updated_at > appeared_at, (
            f"tool step was not updated after it appeared\n{chat.log.describe()}"
        )

    def test_running_state_precedes_the_result(self, chat: ChatPage) -> None:
        """Первым приходит пометка running, а не готовый вывод инструмента."""
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        steps = chat.log.steps_of_type(StepKind.TOOL.value)
        assert len(steps) >= 2, chat.log.describe()
        assert steps[0].get("output") == "running", steps[0]
        assert steps[-1].get("output") != "running", steps[-1]

    def test_is_shown_in_dom(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()
        chat.expand_process()

        step = chat.expand_step(StepKind.TOOL.value)
        assert "stream_logs_usage" in step.inner_text(), step.inner_text()


class TestTurnOrder:
    """Порядок ленты: рассуждения раньше инструмента, инструмент раньше ответа."""

    def test_thinking_precedes_tool_and_answer(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.TOOL.value} please")
        chat.await_idle()

        thinking = chat.log.streamed_steps(StepKind.LLM.value)
        assert thinking, chat.log.describe()

        tools = chat.log.steps_of_type(StepKind.TOOL.value)
        assert tools, chat.log.describe()

        thinking_at = chat.log.index_of(ChatEvent.STREAM_START, thinking[0])
        tool_at = chat.log.index_of(ChatEvent.NEW_MESSAGE, str(tools[0]["id"]))
        answers = chat.log.streamed_steps(StepKind.ASSISTANT.value)
        assert answers, chat.log.describe()

        answer_at = chat.log.index_of(ChatEvent.STREAM_START, answers[0])
        assert thinking_at < tool_at < answer_at, chat.log.describe()
