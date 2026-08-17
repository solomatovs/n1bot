"""Живой вывод инструмента: журнал, слежение сигналами и окна по смещению.

Ключевые инварианты: вывод пишется в файл журнала и переживает конец хода;
фронту уходят только сигналы об изменении — содержимое он запрашивает сам
окнами фиксированного размера; журнал открывает обвязка по tool_call_id.
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
from boba.chainlit.canvas.journal import (
    DirVault,
    JournalWindow,
    StreamJournal,
    StreamKey,
)
from boba.chainlit.canvas.panel import (
    CanvasContent,
    CanvasKind,
    CanvasPanel,
    CanvasSignal,
    CanvasWatch,
    JournalWatchSource,
    StreamActions,
    StreamPath,
    ToolStream,
    ToolStreams,
    WatchProbe,
)
from boba.chainlit.domain.turn import TurnContext
from boba.chainlit.infra.plugins import stream_source
from boba.chainlit.rendering.chat_view import (
    ChatSink,
    ChatView,
    RecordingSink,
    StepRole,
)
from boba.toolkit.channels import CallOutcome, ToolChannel
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
    CanvasWatch.reset()
    ToolStreamTap.set(None)
    # контекст сбрасывается за собой: иначе сессия утечёт в тесты без неё
    context_var.reset(token)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def begin_stream(call_id: str = CALL_ID) -> ToolStream:
    ToolStreams.mark_streamable([TOOL_NAME])
    stream = ToolStreams.begin(USER, THREAD, call_id, TOOL_NAME)
    if stream is None:
        raise AssertionError("stream is not None")
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

        if len(seen) != 1:
            raise AssertionError("len(seen) == 1")
        if seen[0] is None:
            raise AssertionError("seen[0] is not None")

    def test_tool_output_lands_in_the_journal(self) -> None:
        run(self._invoke())

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        if piece is None:
            raise AssertionError("piece is not None")
        if "ran: echo hi" not in piece.text:
            raise AssertionError('"ran: echo hi" in piece.text')

    def test_journal_is_closed_after_the_call(self) -> None:
        run(self._invoke())

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if piece.note != str(CallOutcome.FINISHED):
            raise AssertionError("piece.note == str(CallOutcome.FINISHED)")

    def test_not_streamable_tool_gets_no_recorder(self) -> None:
        seen = run(self._invoke(streamable=False))

        if seen != [None]:
            raise AssertionError("seen == [None]")
        if ToolStreams.get(THREAD, CALL_ID) is not None:
            raise AssertionError("ToolStreams.get(THREAD, CALL_ID) is None")

    def test_failed_call_closes_with_failure_note(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        @tool
        def fake_bash(command: str) -> str:
            """Падает после записи в журнал."""
            sink = ToolStreamTap.get()
            if sink is None:
                raise AssertionError("sink is not None")
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
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if piece.note != str(CallOutcome.FAILED):
            raise AssertionError("piece.note == str(CallOutcome.FAILED)")
        if piece.text != "partial":
            raise AssertionError('piece.text == "partial"')

    def test_parallel_same_name_calls_keep_own_journals(self) -> None:
        """Два одноимённых вызова: каждый пишет в файл своего call_id."""
        ToolStreams.mark_streamable([TOOL_NAME])

        @tool
        def fake_bash(command: str) -> str:
            """Пишет свою команду в свой журнал."""
            sink = ToolStreamTap.get()
            if sink is None:
                raise AssertionError("sink is not None")
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
        if alpha is None:
            raise AssertionError("alpha is not None")
        if beta is None:
            raise AssertionError("beta is not None")
        if alpha.text != "cmd: alpha":
            raise AssertionError('alpha.text == "cmd: alpha"')
        if beta.text != "cmd: beta":
            raise AssertionError('beta.text == "cmd: beta"')


class TestJournalOutlivesTheTurn:
    """Журнал переживает конец хода: история открывает поток заново."""

    def test_slice_after_the_turn_ends(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed("прошлый ход".encode())
        stream.close(str(CallOutcome.FINISHED))
        TurnScope.end()

        if ToolStreams.get(THREAD, CALL_ID) is not None:
            raise AssertionError("ToolStreams.get(THREAD, CALL_ID) is None")

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.text != "прошлый ход":
            raise AssertionError('piece.text == "прошлый ход"')
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")

    def test_turn_end_closes_abandoned_recorder(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"data")

        TurnScope.end()

        piece = ToolStreams.recorded_slice(
            USER, THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if piece.note != CallOutcome.STOPPED.value:
            raise AssertionError("piece.note == CallOutcome.STOPPED.value")

    def test_foreign_user_cannot_read_the_journal(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"secret")
        TurnScope.end()

        piece = ToolStreams.recorded_slice(
            "999", THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        if piece is not None:
            raise AssertionError("piece is None")


class TestBegin:
    """Регистрация живого вызова отвергает небезопасные идентификаторы."""

    def test_unsafe_call_id_is_refused(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        stream = ToolStreams.begin(USER, THREAD, "../../etc/passwd", TOOL_NAME)

        if stream is not None:
            raise AssertionError("stream is None")

    def test_dotted_call_id_is_refused(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        stream = ToolStreams.begin(USER, THREAD, "call.0", TOOL_NAME)

        if stream is not None:
            raise AssertionError("stream is None")


class FakeTransport:
    """Транспорт в тестах: копит payload'ы сигналов вместо сокетов."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.dead = False

    def alive(self, thread_id: str) -> bool:
        return not self.dead

    async def send(self, thread_id: str, payload: Any) -> None:
        self.sent.append(dict(payload))


def _speed_up_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CanvasWatch, "POLL_SEC", 0.05)
    monkeypatch.setattr(CanvasWatch, "COALESCE_SEC", 0.05)


def _journal_source(call_id: str, live: ToolStream | None) -> JournalWatchSource:
    journal = ToolStreams.journal()
    if journal is None:
        raise AssertionError("journal is not None")

    key = StreamKey(user_id=USER, thread_id=THREAD, call_id=call_id)
    return JournalWatchSource(journal, key, STDOUT, live)


async def _watch_finished(timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec

    while CanvasWatch.watching(THREAD) is not None:
        if time.monotonic() > deadline:
            raise AssertionError("watch has not finished in time")
        await asyncio.sleep(0.01)


class TestWatch:
    """Слежение шлёт сигналы об изменении; содержимое по сокету не едет."""

    CHUNKS = 300
    CHUNK = b"x" * 1024

    def _watched_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[FakeTransport, int]:
        _speed_up_watch(monkeypatch)

        async def scenario() -> tuple[FakeTransport, int]:
            transport = FakeTransport()
            CanvasWatch.configure(transport)

            stream = begin_stream()
            source = _journal_source(CALL_ID, stream)
            CanvasWatch.show(
                THREAD, StreamPath.render(CALL_ID), "n-1", source, seen="0:0"
            )

            total = 0

            def write_all() -> None:
                nonlocal total
                sink = stream.sink_of(STDOUT)
                for index in range(self.CHUNKS):
                    data = b"%06d " % index + self.CHUNK
                    sink.feed(data)
                    total += len(data)
                    time.sleep(0.001)
                stream.close(str(CallOutcome.FINISHED))

            writer = threading.Thread(target=write_all)
            writer.start()
            await asyncio.get_running_loop().run_in_executor(None, writer.join)
            await _watch_finished()
            return transport, total

        return run(scenario())

    def test_signals_carry_state_and_no_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport, total = self._watched_writes(monkeypatch)

        if not transport.sent:
            raise AssertionError("transport.sent")
        for payload in transport.sent:
            if payload["type"] != CanvasSignal.TYPE:
                raise AssertionError('payload["type"] == CanvasSignal.TYPE')
            if payload["path"] != StreamPath.render(CALL_ID):
                raise AssertionError('payload["path"] == StreamPath.render(CALL_ID)')
            if "text" in payload:
                raise AssertionError('"text" not in payload')

        final = transport.sent[-1]
        if final["closed"] is not True:
            raise AssertionError('final["closed"] is True')
        if final["size"] != total:
            raise AssertionError('final["size"] == total')
        if final["note"] != str(CallOutcome.FINISHED):
            raise AssertionError('final["note"] == str(CallOutcome.FINISHED)')

    def test_signals_are_coalesced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport, _ = self._watched_writes(monkeypatch)

        if len(transport.sent) >= self.CHUNKS / 10:
            raise AssertionError("len(transport.sent) < self.CHUNKS / 10")

    def test_show_replaces_previous_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            first = begin_stream("call-a")
            second = begin_stream("call-b")

            CanvasWatch.show(
                THREAD,
                StreamPath.render("call-a"),
                "n-a",
                _journal_source("call-a", first),
            )
            CanvasWatch.show(
                THREAD,
                StreamPath.render("call-b"),
                "n-b",
                _journal_source("call-b", second),
            )

            watching = CanvasWatch.watching(THREAD)
            CanvasWatch.drop(THREAD)
            return watching

        watching = run(scenario())
        if watching != StreamPath.render("call-b"):
            raise AssertionError('watching == StreamPath.render("call-b")')

    def test_leave_respects_the_nonce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Гонка переоткрытия: leave старого показа не снимает свежий вотчер."""
        _speed_up_watch(monkeypatch)

        async def scenario() -> tuple[str | None, str | None]:
            CanvasWatch.configure(FakeTransport())
            stream = begin_stream()
            CanvasWatch.show(
                THREAD,
                StreamPath.render(CALL_ID),
                "n-new",
                _journal_source(CALL_ID, stream),
            )

            CanvasWatch.leave(THREAD, "n-old")
            after_foreign = CanvasWatch.watching(THREAD)

            CanvasWatch.leave(THREAD, "n-new")
            after_own = CanvasWatch.watching(THREAD)
            return after_foreign, after_own

        after_foreign, after_own = run(scenario())
        if after_foreign != StreamPath.render(CALL_ID):
            raise AssertionError("after_foreign == StreamPath.render(CALL_ID)")
        if after_own is not None:
            raise AssertionError("after_own is None")

    def test_watch_stops_when_the_room_dies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)

        async def scenario() -> None:
            transport = FakeTransport()
            CanvasWatch.configure(transport)
            stream = begin_stream()
            CanvasWatch.show(
                THREAD,
                StreamPath.render(CALL_ID),
                "n-1",
                _journal_source(CALL_ID, stream),
            )

            transport.dead = True
            await _watch_finished()

        run(scenario())

    def test_file_watch_survives_closed_probes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Файл workspace closed для показа, но не final: слежение живёт.

        Регресс: файловые источники отдавали closed=True, и слежение
        снималось сразу после первой пробы — обновления файлов не доходили.
        """
        _speed_up_watch(monkeypatch)

        class MutableFile:
            def __init__(self) -> None:
                self.revision = "r1"

            async def probe(self) -> WatchProbe:
                return WatchProbe(
                    revision=self.revision, size=1, closed=True, final=False
                )

            def attach_waker(self) -> None:
                return None

        async def scenario() -> tuple[list[dict[str, Any]], str | None]:
            transport = FakeTransport()
            CanvasWatch.configure(transport)
            source = MutableFile()
            CanvasWatch.show(THREAD, "/workspace/t/upload/a.log", "n-1", source)

            await asyncio.sleep(0.2)
            source.revision = "r2"

            deadline = time.monotonic() + 5.0
            while not transport.sent:
                if time.monotonic() > deadline:
                    raise AssertionError("signal has not arrived in time")
                await asyncio.sleep(0.02)

            watching = CanvasWatch.watching(THREAD)
            CanvasWatch.drop(THREAD)
            return transport.sent, watching

        sent, watching = run(scenario())
        if watching != "/workspace/t/upload/a.log":
            raise AssertionError("слежение снялось после closed-пробы файла")
        if sent[0]["revision"] != "r2":
            raise AssertionError('sent[0]["revision"] == "r2"')

    def test_closed_journal_watch_ends_without_signals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Закрытый вызов статичен: слежение снимается без единого сигнала."""
        _speed_up_watch(monkeypatch)

        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"done output")
        stream.close(str(CallOutcome.FINISHED))
        TurnScope.end()

        async def scenario() -> FakeTransport:
            transport = FakeTransport()
            CanvasWatch.configure(transport)
            CanvasWatch.show(
                THREAD,
                StreamPath.render(CALL_ID),
                "n-1",
                _journal_source(CALL_ID, None),
            )
            await _watch_finished()
            return transport

        transport = run(scenario())
        if transport.sent:
            raise AssertionError("not transport.sent")


class TestWindowAction:
    """Окна ходят по журналу по пути показа stream://{call_id}."""

    BODY = ("0123456789" * 20000).encode()
    """200 КБ: больше трёх окон журнала."""

    PATH = StreamPath.render(CALL_ID)

    def _recorded(self) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(self.BODY)
        stream.close(str(CallOutcome.FINISHED))
        TurnScope.end()

    def test_windows_walk_the_journal(self) -> None:
        self._recorded()

        first = run(
            StreamActions.window(USER, THREAD, {"path": self.PATH, "offset": 0})
        )
        middle = run(
            StreamActions.window(USER, THREAD, {"path": self.PATH, "offset": 70000})
        )

        if first["stream"]["offset"] != 0:
            raise AssertionError('first["stream"]["offset"] == 0')
        if first["stream"]["size"] != len(self.BODY):
            raise AssertionError('first["stream"]["size"] == len(self.BODY)')
        if middle["stream"]["offset"] != 70000:
            raise AssertionError('middle["stream"]["offset"] == 70000')
        if len(middle["text"].encode()) != first["stream"]["window"]:
            raise AssertionError('len(middle["text"].encode()) == first["stream"]["wi…')

    def test_tail_window_by_negative_offset(self) -> None:
        self._recorded()

        tail = run(
            StreamActions.window(USER, THREAD, {"path": self.PATH, "offset": -1})
        )

        if tail["stream"]["end"] != len(self.BODY):
            raise AssertionError('tail["stream"]["end"] == len(self.BODY)')
        if len(tail["text"].encode()) > JournalWindow.BYTES:
            raise AssertionError('len(tail["text"].encode()) <= JournalWindow.BYTES')

    def test_window_before_joins_backwards(self) -> None:
        self._recorded()

        before = run(
            StreamActions.window(USER, THREAD, {"path": self.PATH, "before": 70000})
        )

        if before["stream"]["end"] != 70000:
            raise AssertionError('before["stream"]["end"] == 70000')

    def test_offset_beyond_the_file_gives_empty_window(self) -> None:
        self._recorded()

        beyond = run(
            StreamActions.window(USER, THREAD, {"path": self.PATH, "offset": 10**9})
        )

        if beyond["text"] != "":
            raise AssertionError('beyond["text"] == ""')
        if beyond["stream"]["offset"] != len(self.BODY):
            raise AssertionError('beyond["stream"]["offset"] == len(self.BODY)')

    def test_unknown_call_gives_empty_answer(self) -> None:
        answer = run(
            StreamActions.window(
                USER, THREAD, {"path": StreamPath.render("no-such-call"), "offset": 0}
            )
        )

        if answer != {}:
            raise AssertionError("answer == {}")


class PanelProbe:
    """Подмена показа панели: контент копится вместо доставки в chainlit."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.shown: list[CanvasContent] = []

        async def capture(content: CanvasContent) -> None:
            self.shown.append(content)

        monkeypatch.setattr(CanvasPanel, "show", capture)


class TestShowAction:
    """Кнопка потока: окно с начала журнала в панель плюс слежение."""

    def test_recorded_stream_is_shown_from_the_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed("сохранённый вывод".encode())
        stream.close(str(CallOutcome.FINISHED))
        TurnScope.end()

        probe = PanelProbe(monkeypatch)

        run(StreamActions.show(USER, THREAD, {"call_id": CALL_ID}))

        if len(probe.shown) != 1:
            raise AssertionError("len(probe.shown) == 1")
        shown = probe.shown[0]
        if shown.kind is not CanvasKind.STREAM:
            raise AssertionError("shown.kind is CanvasKind.STREAM")
        if "сохранённый вывод" not in shown.text:
            raise AssertionError('"сохранённый вывод" in shown.text')
        if shown.stream is None:
            raise AssertionError("shown.stream is not None")
        if shown.stream.offset != 0:
            raise AssertionError("shown.stream.offset == 0")
        if not shown.nonce:
            raise AssertionError("shown.nonce")

    def test_show_registers_the_watch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _speed_up_watch(monkeypatch)
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"live")

        probe = PanelProbe(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            await StreamActions.show(USER, THREAD, {"call_id": CALL_ID})
            watching = CanvasWatch.watching(THREAD)
            CanvasWatch.drop(THREAD)
            return watching

        watching = run(scenario())
        if watching != StreamPath.render(CALL_ID):
            raise AssertionError("watching == StreamPath.render(CALL_ID)")
        if len(probe.shown) != 1:
            raise AssertionError("len(probe.shown) == 1")

    def test_unknown_stream_is_explained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = PanelProbe(monkeypatch)

        run(StreamActions.show(USER, THREAD, {"call_id": "no-such"}))

        if probe.shown[0].kind is not CanvasKind.NOTICE:
            raise AssertionError("probe.shown[0].kind is CanvasKind.NOTICE")
        if "unavailable" not in probe.shown[0].note:
            raise AssertionError('"unavailable" in probe.shown[0].note')

    def test_leave_action_drops_the_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"live")

        probe = PanelProbe(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            await StreamActions.show(USER, THREAD, {"call_id": CALL_ID})
            nonce = probe.shown[0].nonce

            StreamActions.leave(THREAD, {"path": probe.shown[0].path, "nonce": nonce})
            return CanvasWatch.watching(THREAD)

        watching = run(scenario())
        if watching is not None:
            raise AssertionError("watching is None")


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
        if len(elements) != 1:
            raise AssertionError("len(elements) == 1")
        element = elements[0]
        if element.name != "CanvasStream":
            raise AssertionError('element.name == "CanvasStream"')
        if getattr(element, "props", {}).get("call_id") != CALL_ID:
            raise AssertionError('getattr(element, "props", {}).get("call_id") == CAL…')
        if element.id != ChatView.derive_id(THREAD, CALL_ID, StepRole.STREAM):
            raise AssertionError("element.id == ChatView.derive_id(THREAD, CALL_ID, S…")

    def test_no_journal_means_no_button(self) -> None:
        ToolStreams.reset()
        ToolStreams.mark_streamable([TOOL_NAME])

        step = run(self._tool_step(ElementSink(), TOOL_NAME))

        if step.elements:
            raise AssertionError("not step.elements")

    def test_other_tools_stay_clean(self) -> None:
        sink = ElementSink()

        step = run(self._tool_step(sink, "diagram_save"))

        if step.elements:
            raise AssertionError("not step.elements")

    def test_replay_sink_never_emits_the_button(self) -> None:
        ToolStreams.mark_streamable([TOOL_NAME])

        step = run(self._tool_step(RecordingSink(), TOOL_NAME))

        if step.elements:
            raise AssertionError("not step.elements")

    def test_replayed_step_dict_matches_live(self) -> None:
        """Кнопка не должна ломать контракт шагов: сравниваются StepDict."""
        ToolStreams.mark_streamable([TOOL_NAME])

        live = run(self._tool_step(ElementSink(), TOOL_NAME)).to_dict()
        replay = run(self._tool_step(RecordingSink(), TOOL_NAME)).to_dict()

        if live["id"] != replay["id"]:
            raise AssertionError('live["id"] == replay["id"]')
        if live["name"] != replay["name"]:
            raise AssertionError('live["name"] == replay["name"]')
        if live["parentId"] != replay["parentId"]:
            raise AssertionError('live["parentId"] == replay["parentId"]')


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
        stream.close(str(CallOutcome.FINISHED))

        from httpx import ASGITransport, AsyncClient

        app = self._app(USER, str(tmp_path))

        async def scenario() -> Any:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="https://t") as client:
                whole = await client.get(f"/stream/{THREAD}/{CALL_ID}")
                part = await client.get(
                    f"/stream/{THREAD}/{CALL_ID}", headers={"Range": "bytes=0-9"}
                )
                missing = await client.get(f"/stream/{THREAD}/absent-call")
            return whole, part, missing

        whole, part, missing = run(scenario())

        if whole.status_code != 200:
            raise AssertionError("whole.status_code == 200")
        if whole.content != body.encode():
            raise AssertionError("whole.content == body.encode()")
        if whole.headers["content-length"] != str(len(body.encode())):
            raise AssertionError('whole.headers["content-length"] == str(len(body.enc…')
        if "attachment" not in whole.headers["content-disposition"]:
            raise AssertionError('"attachment" in whole.headers["content-disposition"]')
        if f"{CALL_ID}.tool_stdout.log" not in whole.headers["content-disposition"]:
            raise AssertionError('f"{CALL_ID}.tool_stdout.log" in whole.headers["cont…')

        if part.status_code != 206:
            raise AssertionError("part.status_code == 206")
        if part.content != body.encode()[:10]:
            raise AssertionError("part.content == body.encode()[:10]")

        if missing.status_code != 404:
            raise AssertionError("missing.status_code == 404")

    def test_foreign_user_gets_no_log(self, tmp_path: Path) -> None:
        stream = begin_stream()
        stream.sink_of(STDOUT).feed(b"secret output")
        stream.close(str(CallOutcome.FINISHED))

        from httpx import ASGITransport, AsyncClient

        app = self._app("999", str(tmp_path))

        async def scenario() -> Any:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="https://t") as client:
                return await client.get(f"/stream/{THREAD}/{CALL_ID}")

        response = run(scenario())
        if response.status_code != 404:
            raise AssertionError("response.status_code == 404")
