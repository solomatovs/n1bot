"""Лента чата: раскладка шагов хода."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import mcp_types as mt
import pytest
from chainlit.config import config as chainlit_config
from chainlit.context import ChainlitContext, context_var
from chainlit.step import StepDict

from boba.aid.chat.ui_text import UiText
from boba.aid.domain.fields import StepField
from boba.aid.rendering.chat_view import (
    ChatView,
    LiveSink,
    RecordingSink,
    StepElapsed,
    StepRole,
    StepStatus,
    StepText,
    TurnPulse,
)
from boba.runtime.storage import LocalStorageClient
from boba.stand.refs import StandRefs
from boba.toolkit.result import FieldLines

APP_ROOT = Path(__file__).resolve().parents[1] / "assets"
"""Корень приложения пакета: его переводы chainlit несут подписи ленты."""

TEXTS = UiText(APP_ROOT, UiText.DEFAULT_LANGUAGE)
"""Подписи ленты из переводов приложения пакета на языке по умолчанию."""

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
    """Live обязан давать тот же порядок, что воспроизведение истории."""

    @pytest.mark.anyio
    async def test_tool_seals_the_current_answer(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Текст после инструмента — новое сообщение, иначе элемент тула уедет вниз."""
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        await view.stream_answer("Сейчас нарисую", TURN)
        first = view.answer_message
        if first is None:
            raise AssertionError("first is not None")

        await view.tool_started("diagram_save", {"name": "a.mmd"}, "call-1", None, None)

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
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        seen: list[str] = []
        for index in range(3):
            await view.stream_answer(f"часть {index}", TURN)
            message = view.answer_message
            if message is None:
                raise AssertionError("message is not None")
            seen.append(message.id)
            await view.tool_started("bash", {"cmd": "ls"}, f"call-{index}", None, None)

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
    async def test_pulse_opens_the_turn(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        """До первого токена ход виден кружком, а не пустой лентой."""
        sink = RecordingSink()
        view = ChatView(
            THREAD,
            sink,
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
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
    async def test_answer_stream_holds_the_pulse(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Стримящийся ответ рисует курсор сам: второго кружка быть не должно."""
        sink = RecordingSink()
        view = ChatView(
            THREAD,
            sink,
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        await view.await_model()
        await view.stream_answer("текст", TURN)

        if self._pulse(sink.steps) is not None:
            raise AssertionError("pulse is hidden while the answer streams")

    @pytest.mark.anyio
    async def test_tool_call_keeps_the_pulse_last(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Вызов инструмента закрывает ответ — кружок возвращается под него."""
        sink = RecordingSink()
        view = ChatView(
            THREAD,
            sink,
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        await view.await_model()
        await view.stream_answer("сейчас посмотрю", TURN)
        await view.tool_started("bash", {"cmd": "ls"}, "call-1", None, None)

        if self._pulse(sink.steps) is None:
            raise AssertionError("pulse is back while the tool runs")
        if self._roots(sink.steps)[-1] != PULSE:
            raise AssertionError("pulse is the last step")

    @pytest.mark.anyio
    async def test_finished_turn_has_no_pulse(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Ход закончился — мигать больше нечему."""
        sink = RecordingSink()
        view = ChatView(
            THREAD,
            sink,
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        await view.await_model()
        await view.tool_started("bash", {"cmd": "ls"}, "call-1", None, None)
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
        runtime_stand: StandRefs,
        storage: LocalStorageClient,
        scenario: Callable[[ChatView], Coroutine[Any, Any, None]],
    ) -> FakeEmitter:
        recorded = FakeContext(THREAD)
        token = context_var.set(cast("ChainlitContext", recorded))
        try:
            view = ChatView(
                THREAD,
                LiveSink(),
                "u-1",
                storage,
                TEXTS,
                user_name="Пользователь",
            )
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

    def test_turn_opens_and_closes_the_pulse(
        self, runtime_stand: StandRefs, storage: LocalStorageClient
    ) -> None:
        async def scenario(view: ChatView) -> None:
            await view.await_model()
            await view.stream_answer("сейчас посмотрю", TURN)
            step = await view.tool_started("bash", {"cmd": "ls"}, "call-1", None, None)
            await view.tool_finished(
                step,
                mt.CallToolResult(content=[mt.TextContent(text="ok")]),
                "call-1",
                0,
            )
            await view.close_answer(TURN)
            await view.finish_turn()

        emitter = self._play(runtime_stand, storage, scenario)

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

    def test_pulse_is_sent_after_the_answer(
        self, runtime_stand: StandRefs, storage: LocalStorageClient
    ) -> None:
        """Кружок обязан прийти позже ответа, иначе он висит не в конце ленты."""

        async def scenario(view: ChatView) -> None:
            await view.await_model()
            await view.stream_answer("сейчас посмотрю", TURN)
            await view.tool_started("bash", {"cmd": "ls"}, "call-1", None, None)

        emitter = self._play(runtime_stand, storage, scenario)

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


class TestStepElapsed:
    """Подпись длительности вызова в названии шага, единицами языка."""

    ELAPSED = StepElapsed(TEXTS)
    RUSSIAN = StepElapsed(UiText(APP_ROOT, "ru-RU"))

    def test_milliseconds(self) -> None:
        if self.ELAPSED.of(340) != "340 ms":
            raise AssertionError(self.ELAPSED.of(340))

    def test_seconds(self) -> None:
        if self.ELAPSED.of(1250) != "1.2 s":
            raise AssertionError(self.ELAPSED.of(1250))

    def test_minutes(self) -> None:
        if self.ELAPSED.of(95_000) != "1 m 35 s":
            raise AssertionError(self.ELAPSED.of(95_000))

    def test_units_follow_the_language(self) -> None:
        if self.RUSSIAN.of(95_000) != "1 мин 35 с":
            raise AssertionError(self.RUSSIAN.of(95_000))

    def test_unmeasured_is_silent(self) -> None:
        """Ноль означает, что время не измеряли: в названии его быть не должно."""
        if self.ELAPSED.of(0) != "":
            raise AssertionError(self.ELAPSED.of(0))

        unmeasured = StepStatus.DONE.timed("bash", self.ELAPSED.of(0))
        if unmeasured != StepStatus.DONE.title("bash"):
            raise AssertionError(unmeasured)

    def test_title_keeps_status(self) -> None:
        failed = StepStatus.FAILED.timed("bash", self.ELAPSED.of(2000))
        if failed != "✖ bash · 2.0 s":
            raise AssertionError(failed)


class TestToolStepDuration:
    """Длительность вызова показывается на завершённом шаге инструмента."""

    @pytest.mark.anyio
    async def test_duration_lands_in_step_name(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        step = await view.tool_started(
            "kb_fts_search", {"query": "x"}, "call-1", None, None
        )
        await view.tool_finished(
            step,
            mt.CallToolResult(content=[mt.TextContent(text="hits")]),
            "call-1",
            1500,
        )

        if step.name != "✔ kb_fts_search · 1.5 s":
            raise AssertionError(step.name)

    @pytest.mark.anyio
    async def test_unmeasured_call_keeps_plain_name(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        step = await view.tool_started(
            "kb_fts_search", {"query": "x"}, "call-2", None, None
        )
        await view.tool_finished(
            step, mt.CallToolResult(content=[mt.TextContent(text="hits")]), "call-2", 0
        )

        if step.name != "✔ kb_fts_search":
            raise AssertionError(step.name)


class TestToolIntent:
    """Подпись вызова от LLM: название шага вместо статуса running."""

    @pytest.mark.anyio
    async def test_intent_titles_the_running_step(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        args = {"command": "ls -la", "intent": "смотрю содержимое каталога"}
        step = await view.tool_started("bash", args, "call-1", None, None)

        if step.name != "○ bash · смотрю содержимое каталога":
            raise AssertionError(step.name)
        # без output фронт не рисует секцию шага и не монтирует кнопку потока
        if step.output != TEXTS.text(StepText.RUNNING.value):
            raise AssertionError(f"running обязан быть под подписью: {step.output!r}")

    @pytest.mark.anyio
    async def test_intent_survives_the_result(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        args = {"query": "kerberos", "intent": "ищу настройку kerberos"}
        step = await view.tool_started("kb_fts_search", args, "call-2", None, None)
        await view.tool_finished(
            step,
            mt.CallToolResult(content=[mt.TextContent(text="hits")]),
            "call-2",
            240,
        )

        if step.name != "✔ kb_fts_search · ищу настройку kerberos · 240 ms":
            raise AssertionError(step.name)

    @pytest.mark.anyio
    async def test_intent_is_not_shown_among_arguments(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        """Подпись живёт в названии шага и во вход вызова не попадает."""
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        args = {"query": "kerberos", "intent": "ищу настройку kerberos"}
        step = await view.tool_started("kb_fts_search", args, "call-3", None, None)

        if step.input is None:
            raise AssertionError("вход шага показан")
        if "intent" in step.input:
            raise AssertionError(f"вход шага без подписи: {step.input!r}")

    @pytest.mark.anyio
    async def test_call_without_intent_keeps_the_old_look(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(
            THREAD,
            RecordingSink(),
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        step = await view.tool_started(
            "kb_fts_search", {"query": "kerberos"}, "call-4", None, None
        )

        if step.name != "○ kb_fts_search":
            raise AssertionError(step.name)
        if step.output != TEXTS.text(StepText.RUNNING.value):
            raise AssertionError(f"без подписи шаг остаётся running: {step.output!r}")


def _names(steps: list[StepDict]) -> list[str]:
    """Названия шагов ленты: ключ необязательный, поэтому читается через get."""
    found: list[str] = []
    for step in steps:
        found.append(str(step.get(StepField.NAME, "")))

    return found


class TestContainerAvatar:
    """Фронт грузит аватар корневого шага по имени, а имя контейнера меняется со
    счётчиком: контейнер несёт имя ассистента в metadata, и запрос аватара один."""

    @pytest.mark.anyio
    async def test_container_carries_the_assistant_avatar(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        sink = RecordingSink()
        view = ChatView(
            THREAD,
            sink,
            "u-1",
            storage,
            TEXTS,
            user_name="Пользователь",
        )
        view.begin_turn(TURN)

        await view.container()

        containers = [
            step
            for step in sink.steps
            if step.get(StepField.NAME) == TEXTS.text(StepText.CONTAINER.value)
        ]
        if not containers:
            raise AssertionError("контейнер хода не отправлен")

        avatars = {
            str(step.get(StepField.METADATA, {}).get(ChatView.AVATAR_NAME_KEY))
            for step in containers
        }
        if avatars != {chainlit_config.ui.name}:
            raise AssertionError(f"аватар контейнера: {avatars}")


class TestCallViewOfACard:
    """Вход шага инструмента сервера лента рисует по виду аргументов из его
    карточки: кода инструмента в процессе чата нет."""

    ARGS: dict[str, object] = {"sql": "select 1", "cfg": "internal", "top_k": 7}

    @pytest.mark.anyio
    async def test_step_input_follows_the_content_types_of_the_arguments(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(THREAD, RecordingSink(), "u-1", storage, TEXTS)
        view.begin_turn(TURN)

        step = await view.tool_started(
            "typed_probe", self.ARGS, "call-card-1", {"sql": "application/sql"}, None
        )

        expected = "\n\n".join(
            [
                "```sql\nselect 1\n```",
                FieldLines.line("cfg", "internal"),
                FieldLines.line("top_k", 7),
            ]
        )
        if step.input != expected:
            raise AssertionError(f"the input follows the types: {step.input!r}")

    @pytest.mark.anyio
    async def test_call_without_types_and_model_shows_the_arguments_as_json(
        self, runtime_stand: StandRefs, storage: LocalStorageClient, http_context: None
    ) -> None:
        view = ChatView(THREAD, RecordingSink(), "u-1", storage, TEXTS)
        view.begin_turn(TURN)

        step = await view.tool_started(
            "plain_probe", self.ARGS, "call-card-2", None, None
        )

        if not step.input.startswith("```json"):
            raise AssertionError(f"a tool without views is shown raw: {step.input!r}")
        if '"cfg": "internal"' not in step.input:
            raise AssertionError(f"every argument is shown: {step.input!r}")
