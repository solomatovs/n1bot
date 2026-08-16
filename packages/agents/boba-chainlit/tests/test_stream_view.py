"""Живой вывод инструмента: журнал, доставка call_id через обвязку, насос.

Ключевые инварианты: вывод пишется в файл журнала и переживает конец хода;
фронт получает окна фиксированного размера, сколько бы инструмент ни
напечатал; журнал открывает обвязка по tool_call_id из ToolCall-конверта.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.step import Step
from langchain_core.tools import tool

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.run_log import ToolRunLogger
from boba.chainlit.data.stream_journal import DirVault, StreamJournal
from boba.chainlit.domain.stream import JournalWindow
from boba.chainlit.domain.turn import TurnContext
from boba.chainlit.infra.plugins import stream_source
from boba.chainlit.rendering.canvas import CanvasContent, CanvasKind
from boba.chainlit.rendering.chat_view import (
    ChatSink,
    ChatView,
    RecordingSink,
    StepRole,
)
from boba.chainlit.rendering.stream_view import (
    StreamNote,
    StreamScreen,
    ToolStream,
    ToolStreams,
    window_stream_action,
)
from boba.toolkit.channels import ToolChannel
from boba.toolkit.stream import ToolStreamTap

STDOUT = ToolChannel.STDOUT


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


THREAD = "33333333-3333-3333-3333-333333333333"
USER = "7"
CALL_ID = "call-stream-1"
TOOL_NAME = "fake_bash"


class TurnScope:
    """Контекст хода теста: живые стримы регистрируются в нём и гаснут с ним."""

    _SCOPE: ClassVar[Any] = None

    class Port:
        answer_step_id = "answer-1"

    @classmethod
    def start(cls) -> None:
        cls.end()
        cls._SCOPE = TurnContext.open(THREAD, cls.Port())
        cls._SCOPE.__enter__()

    @classmethod
    def end(cls) -> None:
        """Конец хода: контекст закрывается, файлы журнала остаются на диске."""
        scope = cls._SCOPE
        cls._SCOPE = None
        if scope is not None:
            scope.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def chainlit_context(tmp_path: Path) -> Any:
    """Контекст с thread_id и user сессии, журнал в каталоге на время теста."""
    session = SimpleNamespace(
        id="session-1",
        thread_id=THREAD,
        user=SimpleNamespace(id=USER),
        user_env={},
        chat_settings={},
        chat_profile=None,
        client_type="webapp",
    )
    token = context_var.set(cast("ChainlitContext", SimpleNamespace(session=session)))
    ToolStreams.reset()
    ToolStreams.configure(
        StreamJournal(DirVault(str(tmp_path / "journal")), reserve_bytes=0)
    )
    TurnScope.start()
    yield
    TurnScope.end()
    TurnContext.reset()
    ToolStreams.reset()
    ToolStreamTap.set(None)
    # контекст сбрасывается за собой: иначе сессия утечёт в тесты без неё
    context_var.reset(token)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def begin_stream(call_id: str = CALL_ID) -> ToolStream:
    ToolStreams.mark_streamable([TOOL_NAME])
    stream = ToolStreams.begin(USER, THREAD, call_id, TOOL_NAME)
    assert stream is not None
    return stream


class TestJournalThroughWrapper:
    """Журнал вызова открывает обвязка и доводит тап до функции инструмента.

    Инструмент вызывается как его зовёт ToolNode — ainvoke с ToolCall:
    call_id приезжает синтетическим полем схемы, sync-функция едет в
    executor-поток, где тап обязан отдать приёмник именно этого вызова.
    """

    @staticmethod
    def _tool_and_seen() -> tuple[Any, list[object]]:
        seen: list[object] = []

        @tool
        def fake_bash(command: str) -> str:
            """Пишет в журнал то, что видит в тапе."""
            sink = ToolStreamTap.get()
            seen.append(sink)
            if sink is not None:
                sink.feed(f"ran: {command}".encode())
            return "done"

        ToolCallIdField.attach_all([fake_bash])
        ToolRunLogger.guard_all([fake_bash], stream_source)
        return fake_bash, seen

    async def _invoke(self, *, streamable: bool = True) -> list[object]:
        if streamable:
            ToolStreams.mark_streamable([TOOL_NAME])

        fake_bash, seen = self._tool_and_seen()
        await fake_bash.ainvoke(
            {
                "name": TOOL_NAME,
                "args": {"command": "echo hi"},
                "id": CALL_ID,
                "type": "tool_call",
            }
        )
        return seen

    def test_sync_tool_sees_its_recorder(self) -> None:
        seen = run(self._invoke())

        assert len(seen) == 1
        assert seen[0] is not None

    def test_tool_output_lands_in_the_journal(self) -> None:
        run(self._invoke())

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        assert piece is not None
        assert "ran: echo hi" in piece.text

    def test_journal_is_closed_after_the_call(self) -> None:
        run(self._invoke())

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        assert piece is not None
        assert piece.closed is True
        assert piece.note == str(StreamNote.FINISHED)

    def test_not_streamable_tool_gets_no_recorder(self) -> None:
        seen = run(self._invoke(streamable=False))

        assert seen == [None]
        assert ToolStreams.get(THREAD, CALL_ID) is None

    def test_failed_call_closes_with_failure_note(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        @tool
        def fake_bash(command: str) -> str:
            """Падает после записи в журнал."""
            sink = ToolStreamTap.get()
            assert sink is not None
            sink.feed(b"partial")
            msg = "boom"
            raise RuntimeError(msg)

        ToolCallIdField.attach_all([fake_bash])
        ToolRunLogger.guard_all([fake_bash], stream_source)

        async def scenario() -> None:
            with pytest.raises(RuntimeError):
                await fake_bash.ainvoke(
                    {
                        "name": TOOL_NAME,
                        "args": {"command": "x"},
                        "id": CALL_ID,
                        "type": "tool_call",
                    }
                )

        run(scenario())

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        assert piece is not None
        assert piece.closed is True
        assert piece.note == str(StreamNote.FAILED)
        assert piece.text == "partial"

    def test_parallel_same_name_calls_keep_own_journals(self) -> None:
        """Два одноимённых вызова: каждый пишет в файл своего call_id."""
        ToolStreams.mark_streamable([TOOL_NAME])

        @tool
        def fake_bash(command: str) -> str:
            """Пишет свою команду в свой журнал."""
            sink = ToolStreamTap.get()
            assert sink is not None
            sink.feed(f"cmd: {command}".encode())
            return "done"

        ToolCallIdField.attach_all([fake_bash])
        ToolRunLogger.guard_all([fake_bash], stream_source)

        async def scenario() -> None:
            first = fake_bash.ainvoke(
                {
                    "name": TOOL_NAME,
                    "args": {"command": "alpha"},
                    "id": "call-a",
                    "type": "tool_call",
                }
            )
            second = fake_bash.ainvoke(
                {
                    "name": TOOL_NAME,
                    "args": {"command": "beta"},
                    "id": "call-b",
                    "type": "tool_call",
                }
            )
            await asyncio.gather(first, second)

        run(scenario())

        alpha = ToolStreams.recorded_slice(
            USER, THREAD, "call-a", offset=0, channel=STDOUT
        )
        beta = ToolStreams.recorded_slice(
            USER, THREAD, "call-b", offset=0, channel=STDOUT
        )
        assert alpha is not None
        assert beta is not None
        assert alpha.text == "cmd: alpha"
        assert beta.text == "cmd: beta"


class TestJournalOutlivesTheTurn:
    """Журнал переживает конец хода: история открывает поток заново."""

    def test_slice_after_the_turn_ends(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed("прошлый ход".encode())
        stream.close(str(StreamNote.FINISHED))
        TurnScope.end()

        assert ToolStreams.get(THREAD, CALL_ID) is None

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        assert piece is not None
        assert piece.text == "прошлый ход"
        assert piece.closed is True

    def test_turn_end_closes_abandoned_recorder(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"data")

        TurnScope.end()

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        assert piece is not None
        assert piece.closed is True
        assert piece.note == TurnContext.STREAM_STOP_NOTE

    def test_foreign_user_cannot_read_the_journal(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"secret")
        TurnScope.end()

        piece = ToolStreams.recorded_slice(
            "999", THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        assert piece is None


class TestBegin:
    """Регистрация живого вызова отвергает небезопасные идентификаторы."""

    def test_unsafe_call_id_is_refused(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        stream = ToolStreams.begin(USER, THREAD, "../../etc/passwd", TOOL_NAME)

        assert stream is None

    def test_dotted_call_id_is_refused(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        stream = ToolStreams.begin(USER, THREAD, "call.0", TOOL_NAME)

        assert stream is None


class RecordingChannel:
    """Канал в тестах: копит снапшоты вместо доставки в панель."""

    def __init__(self) -> None:
        self.contents: list[CanvasContent] = []

    async def push(self, content: CanvasContent) -> None:
        self.contents.append(content)


class TestPump:
    """Насос переносит хвост журнала по пробуждениям, не накапливая вывод."""

    CHUNKS = 300
    CHUNK = b"x" * 1024

    async def _stream_with_writer(self) -> tuple[ToolStream, threading.Thread]:
        stream = begin_stream()

        def write_all() -> None:
            sink = stream.sink_of(STDOUT)
            for index in range(self.CHUNKS):
                sink.feed(b"%06d " % index + self.CHUNK)
                time.sleep(0.002)
            stream.close(str(StreamNote.FINISHED))

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
            assert len(content.text.encode()) <= JournalWindow.BYTES

    def test_pushes_are_coalesced(self) -> None:
        channel = run(self._pumped())

        assert len(channel.contents) < self.CHUNKS / 10

    def test_final_push_carries_the_tail_and_position(self) -> None:
        channel = run(self._pumped())

        final = channel.contents[-1]
        assert final.kind is CanvasKind.STREAM
        assert f"{self.CHUNKS - 1:06d}" in final.text
        assert str(StreamNote.FINISHED) in final.note
        assert final.stream is not None
        assert final.stream.closed is True
        assert final.stream.size == self.CHUNKS * (len(self.CHUNK) + 7)
        assert final.stream.offset + final.stream.window >= final.stream.size

    def test_show_replaces_the_previous_pump(self) -> None:
        async def scenario() -> tuple[asyncio.Task[None], asyncio.Task[None]]:
            first = begin_stream("call-a")
            second = begin_stream("call-b")
            channel = RecordingChannel()

            first_task = await StreamScreen.show(THREAD, first, channel)
            second_task = await StreamScreen.show(THREAD, second, channel)

            second.close("done")
            await asyncio.wait_for(second_task, timeout=5)
            return first_task, second_task

        first_task, second_task = run(scenario())

        assert first_task.cancelled() is True
        assert second_task is not first_task

    def test_leave_stops_the_pump(self) -> None:
        async def scenario() -> asyncio.Task[None]:
            stream = begin_stream()
            task = await StreamScreen.show(THREAD, stream, RecordingChannel())

            StreamScreen.leave(THREAD)
            await asyncio.gather(task, return_exceptions=True)
            return task

        task = run(scenario())
        assert task.cancelled() is True


class TestWindowAction:
    """Перемотка ходит по журналу окнами и снимает насос."""

    BODY = ("0123456789" * 20000).encode()
    """200 КБ: больше трёх окон журнала."""

    def _recorded(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(self.BODY)
        stream.close(str(StreamNote.FINISHED))
        TurnScope.end()

    def test_windows_walk_the_journal(self) -> None:
        self._recorded()

        first = run(
            window_stream_action(USER, THREAD, {"call_id": CALL_ID, "offset": 0})
        )
        middle = run(
            window_stream_action(USER, THREAD, {"call_id": CALL_ID, "offset": 70000})
        )

        assert first["stream"]["offset"] == 0
        assert first["stream"]["size"] == len(self.BODY)
        assert middle["stream"]["offset"] == 70000
        assert len(middle["text"].encode()) == first["stream"]["window"]

    def test_offset_beyond_the_file_gives_empty_window(self) -> None:
        self._recorded()

        beyond = run(
            window_stream_action(USER, THREAD, {"call_id": CALL_ID, "offset": 10**9})
        )

        assert beyond["text"] == ""
        assert beyond["stream"]["offset"] == len(self.BODY)

    def test_unknown_call_gives_empty_answer(self) -> None:
        answer = run(
            window_stream_action(USER, THREAD, {"call_id": "no-such-call", "offset": 0})
        )

        assert answer == {}

    def test_window_request_stops_the_pump(self) -> None:
        async def scenario() -> asyncio.Task[None]:
            stream = begin_stream()
            stream.sink_of(STDOUT).feed(b"live data")
            task = await StreamScreen.show(THREAD, stream, RecordingChannel())

            await window_stream_action(USER, THREAD, {"call_id": CALL_ID, "offset": 0})
            await asyncio.gather(task, return_exceptions=True)
            return task

        task = run(scenario())
        assert task.cancelled() is True


class TestShowAction:
    """Кнопка потока: живой вызов — насос, завершённый — окно из журнала."""

    def test_recorded_stream_is_shown_from_the_journal(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed("сохранённый вывод".encode())
        stream.close(str(StreamNote.FINISHED))
        TurnScope.end()

        channel = RecordingChannel()

        async def scenario() -> None:
            piece = ToolStreams.recorded_slice(
                USER, THREAD, CALL_ID, offset=-1, channel=STDOUT
            )
            assert piece is not None
            await StreamScreen.recorded(THREAD, CALL_ID, piece, channel, follow=True)

        run(scenario())

        assert len(channel.contents) == 1
        shown = channel.contents[0]
        assert shown.kind is CanvasKind.STREAM
        assert "сохранённый вывод" in shown.text

    def test_unknown_stream_is_explained(self) -> None:
        channel = RecordingChannel()

        async def scenario() -> None:
            await StreamScreen.gone("no-such", channel)

        run(scenario())

        assert channel.contents[0].kind is CanvasKind.NOTICE
        assert "unavailable" in channel.contents[0].note


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

    def test_no_journal_means_no_button(self) -> None:
        ToolStreams.reset()
        ToolStreams.mark_streamable([TOOL_NAME])

        step = run(self._tool_step(ElementSink(), TOOL_NAME))

        assert not step.elements

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


class TestStreamDownload:
    """Скачивание журнала вызова: тот же StreamedFile, что отдаёт вложения."""

    @staticmethod
    def _app(user_id: str, base_dir: str) -> Any:
        from chainlit.auth import get_current_user
        from chainlit.user import PersistedUser
        from fastapi import FastAPI

        from boba.chainlit.data.upload import StreamServing, UploadPolicy
        from boba.chainlit.domain.keys import StreamUrl
        from boba.chainlit.infra.config import LocalStorageConfig

        # files_dir на серве подменяется корнем тома пользователя; здесь нужен
        # лишь валидный конфиг — LocalStorageConfig требует непустой files_dir
        config = LocalStorageConfig.model_validate(
            {
                "kind": "local",
                "files_dir": base_dir,
                "launcher": {
                    "mount_wait_sec": 1.0,
                    "mount_poll_sec": 0.1,
                    "shutdown_wait_sec": 1.0,
                    "lock_wait_sec": 10.0,
                    "copy_chunk_bytes": 65536,
                },
                "binaries": {"dirs": _bin_dirs()},
            }
        )
        serving = StreamServing(config, UploadPolicy())

        app = FastAPI()
        app.add_api_route(StreamUrl.ROUTE, serving.serve, methods=["GET"])
        user = PersistedUser(
            id=user_id, identifier="tester", createdAt="2024-01-01T00:00:00Z"
        )
        app.dependency_overrides[get_current_user] = lambda: user
        return app

    def test_log_downloads_whole_and_by_range(self, tmp_path: Path) -> None:
        stream = begin_stream()
        body = "строка вывода\n" * 20
        stream.sink_of(STDOUT).feed(body.encode())
        stream.close(str(StreamNote.FINISHED))

        from httpx import ASGITransport, AsyncClient

        app = self._app(USER, str(tmp_path))

        async def scenario() -> Any:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                whole = await client.get(f"/stream/{THREAD}/{CALL_ID}")
                part = await client.get(
                    f"/stream/{THREAD}/{CALL_ID}", headers={"Range": "bytes=0-9"}
                )
                missing = await client.get(f"/stream/{THREAD}/absent-call")
            return whole, part, missing

        whole, part, missing = run(scenario())

        assert whole.status_code == 200
        assert whole.content == body.encode()
        assert whole.headers["content-length"] == str(len(body.encode()))
        assert "attachment" in whole.headers["content-disposition"]
        assert f"{CALL_ID}.tool_stdout.log" in whole.headers["content-disposition"]

        assert part.status_code == 206
        assert part.content == body.encode()[:10]

        assert missing.status_code == 404

    def test_foreign_user_gets_no_log(self, tmp_path: Path) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"secret output")
        stream.close(str(StreamNote.FINISHED))

        from httpx import ASGITransport, AsyncClient

        app = self._app("999", str(tmp_path))

        async def scenario() -> Any:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                return await client.get(f"/stream/{THREAD}/{CALL_ID}")

        response = run(scenario())
        assert response.status_code == 404
