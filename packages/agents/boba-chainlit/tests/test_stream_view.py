"""Живой вывод инструмента: тап через реальный langchain-вызов, насос, кнопка.

Ключевые инварианты: окно держит хвост фиксированного размера, сколько бы
инструмент ни напечатал; тап доезжает до функции тула через обвязку в его
потоке — колбэки langchain для sync-тулов контекст не доносят.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from uuid import uuid4

import pytest
from chainlit.context import ChainlitContext, context_var
from langchain.tools import tool
from langchain_core.messages import ToolMessage

from boba.chainlit.agent.tools.stream_tap import ToolStreamTapGuard
from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.rendering.canvas import CanvasContent, CanvasKind
from boba.chainlit.rendering.chat_view import ChatSink, ChatView, RecordingSink, StepRole
from boba.chainlit.rendering.stream_view import (
    StreamNote,
    StreamScreen,
    ToolStream,
    ToolStreams,
)
from boba.toolkit.stream import ToolStreamTap
from chainlit.step import Step

THREAD = "33333333-3333-3333-3333-333333333333"
CALL_ID = "call-stream-1"
TOOL_NAME = "fake_bash"


@pytest.fixture(autouse=True)
def chainlit_context() -> Any:
    """Контекст с thread_id сессии: его читают обвязка тапа и трейсер."""
    session = SimpleNamespace(thread_id=THREAD)
    context_var.set(cast("ChainlitContext", SimpleNamespace(session=session)))
    ToolStreams.reset()
    yield
    ToolStreams.reset()
    ToolStreamTap.set(None)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestTapThroughLangchain:
    """Окно вызова доезжает до sync-функции инструмента.

    Инструмент вызывается как его зовёт ToolNode — ainvoke с ToolCall и
    callbacks; sync-функция едет в executor-поток, где обвязка тапа обязана
    отдать буфер этого вызова.
    """

    @staticmethod
    def _tool_and_seen() -> tuple[Any, list[object]]:
        seen: list[object] = []

        @tool
        def fake_bash(command: str) -> str:
            """Пишет в окно живого вывода то, что видит в тапе."""
            buffer = ToolStreamTap.get()
            seen.append(buffer)
            if buffer is not None:
                buffer.feed(f"ran: {command}".encode())
            return "done"

        ToolStreamTapGuard.guard_all([fake_bash])
        return fake_bash, seen

    async def _invoke(self) -> list[object]:
        ToolStreams.mark_streamable([TOOL_NAME])
        view = ChatView(THREAD, RecordingSink(), user_name="tester")
        view.begin_turn("turn-1")
        tracer = AgentTracer(view)

        fake_bash, seen = self._tool_and_seen()
        await fake_bash.ainvoke(
            {
                "name": TOOL_NAME,
                "args": {"command": "echo hi"},
                "id": CALL_ID,
                "type": "tool_call",
            },
            config={"callbacks": [tracer]},
        )
        return seen

    def test_sync_tool_sees_its_buffer(self) -> None:
        seen = run(self._invoke())

        assert len(seen) == 1
        assert seen[0] is not None

    def test_tool_output_lands_in_the_window(self) -> None:
        run(self._invoke())

        stream = ToolStreams.get(THREAD, CALL_ID)
        assert stream is not None
        assert "ran: echo hi" in stream.buffer.snapshot().text

    def test_window_is_closed_after_the_call(self) -> None:
        run(self._invoke())

        stream = ToolStreams.get(THREAD, CALL_ID)
        assert stream is not None
        window = stream.buffer.snapshot()
        assert window.closed is True
        assert window.note == str(StreamNote.FINISHED)

    def test_not_streamable_tool_gets_no_buffer(self) -> None:
        async def invoke() -> list[object]:
            view = ChatView(THREAD, RecordingSink(), user_name="tester")
            view.begin_turn("turn-1")
            tracer = AgentTracer(view)

            fake_bash, seen = self._tool_and_seen()
            await fake_bash.ainvoke(
                {
                    "name": TOOL_NAME,
                    "args": {"command": "echo hi"},
                    "id": CALL_ID,
                    "type": "tool_call",
                },
                config={"callbacks": [tracer]},
            )
            return seen

        seen = run(invoke())

        assert seen == [None]
        assert ToolStreams.get(THREAD, CALL_ID) is None

    def test_stop_pending_closes_open_windows(self) -> None:
        async def scenario() -> None:
            ToolStreams.mark_streamable([TOOL_NAME])
            view = ChatView(THREAD, RecordingSink(), user_name="tester")
            view.begin_turn("turn-1")
            tracer = AgentTracer(view)

            await tracer.on_tool_start(
                {"name": TOOL_NAME},
                "{}",
                run_id=uuid4(),
                inputs={},
                tool_call_id=CALL_ID,
            )
            await tracer.stop_pending("остановлено пользователем")

        run(scenario())

        stream = ToolStreams.get(THREAD, CALL_ID)
        assert stream is not None
        window = stream.buffer.snapshot()
        assert window.closed is True
        assert window.note == str(StreamNote.STOPPED)


class TestPendingSlots:
    """Очередь claim не отдаёт чужие и завершённые слоты."""

    def test_finished_slot_is_not_claimable(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])
        ToolStreams.begin(THREAD, CALL_ID, TOOL_NAME)

        ToolStreams.finish(THREAD, CALL_ID, str(StreamNote.FAILED))

        assert ToolStreams.claim(THREAD, TOOL_NAME) is None

    def test_claim_is_per_thread_and_name(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])
        ToolStreams.begin(THREAD, CALL_ID, TOOL_NAME)

        assert ToolStreams.claim("другой-тред", TOOL_NAME) is None
        assert ToolStreams.claim(THREAD, "другой-тул") is None

        stream = ToolStreams.claim(THREAD, TOOL_NAME)
        assert stream is not None
        assert stream.call_id == CALL_ID
        assert ToolStreams.claim(THREAD, TOOL_NAME) is None


class RecordingChannel:
    """Канал в тестах: копит снапшоты вместо доставки в панель."""

    def __init__(self) -> None:
        self.contents: list[CanvasContent] = []

    async def push(self, content: CanvasContent) -> None:
        self.contents.append(content)


class TestPump:
    """Насос переносит хвост окна по пробуждениям, не накапливая вывод."""

    CHUNKS = 300
    CHUNK = b"x" * 1024

    async def _stream_with_writer(self) -> tuple[ToolStream, threading.Thread]:
        stream = ToolStream(THREAD, CALL_ID, TOOL_NAME)

        def write_all() -> None:
            for index in range(self.CHUNKS):
                stream.buffer.feed(b"%06d " % index + self.CHUNK)
                time.sleep(0.002)
            stream.buffer.close(str(StreamNote.FINISHED))

        return stream, threading.Thread(target=write_all)

    async def _pumped(self) -> RecordingChannel:
        """Писатель работает в своём потоке параллельно насосу — как инструмент."""
        stream, writer = await self._stream_with_writer()
        channel = RecordingChannel()

        task = await StreamScreen.show(THREAD, stream, channel)
        writer.start()
        await asyncio.get_running_loop().run_in_executor(None, writer.join)
        await asyncio.wait_for(task, timeout=5)
        return channel

    def test_snapshots_stay_within_the_window(self) -> None:
        channel = run(self._pumped())

        assert channel.contents
        for content in channel.contents:
            assert len(content.text.encode()) <= ToolStream.WINDOW_BYTES

    def test_pushes_are_coalesced(self) -> None:
        channel = run(self._pumped())

        assert len(channel.contents) < self.CHUNKS / 10

    def test_final_push_carries_the_tail_and_the_note(self) -> None:
        channel = run(self._pumped())

        final = channel.contents[-1]
        assert final.kind is CanvasKind.STREAM
        assert ("%06d" % (self.CHUNKS - 1)) in final.text
        assert str(StreamNote.FINISHED) in final.note
        assert "вытеснено" in final.note

    def test_show_replaces_the_previous_pump(self) -> None:
        async def scenario() -> tuple[asyncio.Task[None], asyncio.Task[None]]:
            first = ToolStream(THREAD, "call-a", TOOL_NAME)
            second = ToolStream(THREAD, "call-b", TOOL_NAME)
            channel = RecordingChannel()

            first_task = await StreamScreen.show(THREAD, first, channel)
            second_task = await StreamScreen.show(THREAD, second, channel)

            second.buffer.close("done")
            await asyncio.wait_for(second_task, timeout=5)
            return first_task, second_task

        first_task, second_task = run(scenario())

        assert first_task.cancelled() is True
        assert second_task is not first_task

    def test_leave_stops_the_pump(self) -> None:
        async def scenario() -> asyncio.Task[None]:
            stream = ToolStream(THREAD, CALL_ID, TOOL_NAME)
            task = await StreamScreen.show(THREAD, stream, RecordingChannel())

            StreamScreen.leave(THREAD)
            await asyncio.gather(task, return_exceptions=True)
            return task

        task = run(scenario())
        assert task.cancelled() is True


class ElementSink(ChatSink):
    """Live-подобный sink: элементы включены, шаги копятся в память."""

    EMITS_ELEMENTS: ClassVar[bool] = True

    def __init__(self) -> None:
        self.steps: list[Step] = []

    async def put(self, step: Step) -> None:
        self.steps.append(step)


class TestStreamButton:
    """Кнопка потока живёт на шаге потокового тула и адресуется по call_id."""

    async def _tool_step(self, sink: ChatSink, name: str) -> Step:
        view = ChatView(THREAD, sink, user_name="tester")
        view.begin_turn("turn-1")
        return await view.tool_started(name, {"command": "ls"}, CALL_ID)

    def test_streamable_tool_gets_the_button(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])
        sink = ElementSink()

        step = run(self._tool_step(sink, TOOL_NAME))

        elements = step.elements or []
        assert len(elements) == 1
        element = elements[0]
        assert element.name == "CanvasStream"
        assert getattr(element, "props", {}).get("call_id") == CALL_ID
        assert element.id == ChatView.derive_id(THREAD, CALL_ID, StepRole.STREAM)

    def test_other_tools_stay_clean(self) -> None:
        sink = ElementSink()

        step = run(self._tool_step(sink, "diagram_save"))

        assert not step.elements

    def test_replay_sink_never_emits_the_button(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        step = run(self._tool_step(RecordingSink(), TOOL_NAME))

        assert not step.elements

    def test_replayed_step_dict_matches_live(self) -> None:
        """Кнопка не должна ломать контракт шагов: сравниваются StepDict."""
        ToolStreams.mark_streamable([TOOL_NAME])

        live = run(self._tool_step(ElementSink(), TOOL_NAME)).to_dict()
        replay = run(self._tool_step(RecordingSink(), TOOL_NAME)).to_dict()

        assert live["id"] == replay["id"]
        assert live["name"] == replay["name"]
        assert live["parentId"] == replay["parentId"]


class TestToolMessageFlow:
    """on_tool_end закрывает окно и на пути реального ToolMessage."""

    def test_tool_end_closes_by_run_id(self) -> None:
        async def scenario() -> None:
            ToolStreams.mark_streamable([TOOL_NAME])
            view = ChatView(THREAD, RecordingSink(), user_name="tester")
            view.begin_turn("turn-1")
            tracer = AgentTracer(view)

            tool_run = uuid4()
            await tracer.on_tool_start(
                {"name": TOOL_NAME},
                "{}",
                run_id=tool_run,
                inputs={},
                tool_call_id=CALL_ID,
            )
            await tracer.on_tool_end(
                ToolMessage(content="ok", tool_call_id=CALL_ID, id="tool-msg"),
                run_id=tool_run,
            )

        run(scenario())

        stream = ToolStreams.get(THREAD, CALL_ID)
        assert stream is not None
        assert stream.buffer.snapshot().closed is True
