"""Лента чата: раскладка шагов хода."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.step import StepDict

from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.chat_view import (
    ChatView,
    LiveSink,
    RecordingSink,
    StepElapsed,
    StepRole,
    StepStatus,
    TurnPulse,
)
from boba.toolkit.result import TextResult

THREAD = "11111111-1111-1111-1111-111111111111"
TURN = "22222222-2222-2222-2222-222222222222"
PULSE = ChatView.derive_id(THREAD, TURN, StepRole.PULSE)
ANSWER = ChatView.derive_id(THREAD, TURN, StepRole.ANSWER)


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


class ChatEvent(StrEnum):
    """События ленты, которые chainlit шлёт вкладке."""

    NEW = "new_message"
    UPDATE = "update_message"
    DELETE = "delete_message"
    STREAM_START = "stream_start"
    STREAM_TOKEN = "stream_token"


@dataclass
class Frame:
    """Кадр эмиссии: что и про какой шаг ушло вкладке."""

    event: ChatEvent
    step_id: str


class FakeSession:
    """Сессия для Message и chat_context: им нужны только идентификаторы."""

    def __init__(self, thread_id: str) -> None:
        self.id = "session-1"
        self.thread_id = thread_id


class FakeEmitter:
    """Эмиттер-рекордер: кадры ленты копятся вместо отправки в сокет."""

    def __init__(self) -> None:
        self.frames: list[Frame] = []

    def of(self, event: ChatEvent, step_id: str) -> None:
        self.frames.append(Frame(event=event, step_id=step_id))

    def ids(self, event: ChatEvent) -> list[str]:
        seen: list[str] = []
        for frame in self.frames:
            if frame.event is not event:
                continue

            seen.append(frame.step_id)

        return seen

    async def send_step(self, step_dict: StepDict) -> None:
        self.of(ChatEvent.NEW, str(step_dict.get(StepField.ID, "")))

    async def update_step(self, step_dict: StepDict) -> None:
        self.of(ChatEvent.UPDATE, str(step_dict.get(StepField.ID, "")))

    async def delete_step(self, step_dict: StepDict) -> None:
        self.of(ChatEvent.DELETE, str(step_dict.get(StepField.ID, "")))

    async def stream_start(self, step_dict: StepDict) -> None:
        self.of(ChatEvent.STREAM_START, str(step_dict.get(StepField.ID, "")))

    async def send_token(
        self,
        id: str,  # noqa: A002 — chainlit зовёт по имени: emitter-контракт
        token: str,
        is_sequence: bool = False,
        is_input: bool = False,
    ) -> None:
        self.of(ChatEvent.STREAM_TOKEN, id)


class FakeContext:
    """Контекст хода: ленте нужны только сессия и эмиттер."""

    def __init__(self, thread_id: str) -> None:
        self.session = FakeSession(thread_id)
        self.emitter = FakeEmitter()




class TestLivePulseFrames:
    """Живая лента: кадры пульса приходят вкладке ровно на границах ожидания.

    Контекст ставится в теле теста, а ход гоняется asyncio.run: корутина
    наследует контекст вызывающего, поэтому кадры не зависят от порядка
    тестов в сессии.
    """

    @staticmethod
    def _play(
        scenario: Callable[[ChatView], Coroutine[Any, Any, None]],
    ) -> FakeEmitter:
        recorded = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", recorded))
        try:
            view = ChatView(THREAD, LiveSink(), user_name="Пользователь")
            view.begin_turn(TURN)
            asyncio.run(scenario(view))
        finally:
            context_var.reset(token)

        return recorded.emitter

    @staticmethod
    def _pulse_events(emitter: FakeEmitter) -> list[ChatEvent]:
        events: list[ChatEvent] = []
        for frame in emitter.frames:
            if frame.step_id != PULSE:
                continue

            events.append(frame.event)

        return events

    def test_turn_opens_and_closes_the_pulse(self) -> None:
        async def scenario(view: ChatView) -> None:
            await view.await_model()
            await view.stream_answer("сейчас посмотрю", TURN)
            step = await view.tool_started("bash", {"cmd": "ls"}, "call-1")
            await view.tool_finished(step, TextResult(text="ok"), "call-1")
            await view.close_answer(TURN)
            await view.finish_turn()

        emitter = self._play(scenario)

        expected = [
            ChatEvent.NEW,
            ChatEvent.DELETE,
            ChatEvent.NEW,
            ChatEvent.DELETE,
        ]
        if self._pulse_events(emitter) != expected:
            raise AssertionError(
                f"pulse frames are {self._pulse_events(emitter)}, expected {expected}"
            )

    def test_pulse_is_sent_after_the_answer(self) -> None:
        """Кружок обязан прийти позже ответа, иначе он висит не в конце ленты."""

        async def scenario(view: ChatView) -> None:
            await view.await_model()
            await view.stream_answer("сейчас посмотрю", TURN)
            await view.tool_started("bash", {"cmd": "ls"}, "call-1")

        emitter = self._play(scenario)

        answer_at = -1
        pulse_at = -1
        for index, frame in enumerate(emitter.frames):
            if frame.event is not ChatEvent.NEW:
                continue

            if frame.step_id == PULSE:
                pulse_at = index
                continue

            if frame.step_id == ANSWER:
                answer_at = index

        if answer_at < 0:
            raise AssertionError(f"the sealed answer is sent: {emitter.frames}")
        if pulse_at < answer_at:
            raise AssertionError("pulse follows the answer it waits under")


class TestPrefetchStage:
    """Этап подготовки: шаги поиска вкладываются в него, а не в контейнер."""

    @pytest.mark.anyio
    async def test_tools_nest_into_the_open_stage(self, http_context: None) -> None:
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        stage = await view.begin_stage("context lookup")
        inside = await view.tool_started("kb_fts_search", {"query": "kerberos"}, "c-1")
        await view.end_stage(["kerberos"])
        outside = await view.tool_started("kb_fts_search", {"query": "later"}, "c-2")

        container = view.container_step
        if container is None:
            raise AssertionError("контейнер хода открыт")

        if stage.parent_id != container.id:
            raise AssertionError("этап лежит в контейнере хода")
        if inside.parent_id != stage.id:
            raise AssertionError("шаг подготовки лежит в этапе")
        if outside.parent_id != container.id:
            raise AssertionError("после этапа шаги снова идут в контейнер")

    @pytest.mark.anyio
    async def test_stage_output_lists_the_queries(self, http_context: None) -> None:
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        await view.begin_stage("context lookup")
        await view.end_stage(["первый запрос", "второй запрос"])

        if view.stage_step is not None:
            raise AssertionError("закрытый этап больше не принимает шаги")

        output = ChatView.stage_output(["первый запрос", "второй запрос"])
        if output != "- первый запрос\n- второй запрос":
            raise AssertionError(f"подпись этапа: {output!r}")

    @pytest.mark.anyio
    async def test_stage_id_is_derived_from_the_turn(self, http_context: None) -> None:
        """Live и сборка истории обязаны дать этапу один id."""
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        stage = await view.begin_stage("context lookup")

        if stage.id != ChatView.derive_id(THREAD, TURN, StepRole.STAGE):
            raise AssertionError("id этапа выводится из ключа хода")


class TestStepElapsed:
    """Подпись длительности вызова в названии шага."""

    def test_milliseconds(self) -> None:
        if StepElapsed.of(340) != "340 ms":
            raise AssertionError(StepElapsed.of(340))

    def test_seconds(self) -> None:
        if StepElapsed.of(1250) != "1.2 s":
            raise AssertionError(StepElapsed.of(1250))

    def test_minutes(self) -> None:
        if StepElapsed.of(95_000) != "1 m 35 s":
            raise AssertionError(StepElapsed.of(95_000))

    def test_unmeasured_is_silent(self) -> None:
        """Ноль означает, что время не измеряли: в названии его быть не должно."""
        if StepElapsed.of(0) != "":
            raise AssertionError(StepElapsed.of(0))

        if StepStatus.DONE.timed("bash", 0) != StepStatus.DONE.title("bash"):
            raise AssertionError(StepStatus.DONE.timed("bash", 0))

    def test_title_keeps_status(self) -> None:
        if StepStatus.FAILED.timed("bash", 2000) != "✖ bash · 2.0 s":
            raise AssertionError(StepStatus.FAILED.timed("bash", 2000))


class TestToolStepDuration:
    """Длительность вызова показывается на завершённом шаге инструмента."""

    @pytest.mark.anyio
    async def test_duration_lands_in_step_name(self, http_context: None) -> None:
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        step = await view.tool_started("kb_fts_search", {"query": "x"}, "call-1")
        await view.tool_finished(
            step, TextResult(text="hits", elapsed_ms=1500), "call-1"
        )

        if step.name != "✔ kb_fts_search · 1.5 s":
            raise AssertionError(step.name)

    @pytest.mark.anyio
    async def test_unmeasured_call_keeps_plain_name(self, http_context: None) -> None:
        view = ChatView(THREAD, RecordingSink(), user_name="Пользователь")
        view.begin_turn(TURN)

        step = await view.tool_started("kb_fts_search", {"query": "x"}, "call-2")
        await view.tool_finished(step, TextResult(text="hits"), "call-2")

        if step.name != "✔ kb_fts_search":
            raise AssertionError(step.name)
