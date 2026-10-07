"""Живой вывод инструмента: журнал, слежение сигналами и окна по смещению.

Ключевые инварианты: вывод пишется в файл журнала и переживает конец хода;
фронту уходят только сигналы об изменении — содержимое он запрашивает сам
окнами фиксированного размера; журнал открывает обвязка по tool_call_id.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.step import Step
from chainlit_stand import FakeTurn, RemoteStand, ServiceProcess
from pydantic import ValidationError

from boba.canvas.canvas import (
    CanvasContent,
    CanvasKind,
    CanvasSignal,
    CanvasWatch,
    SignalTransport,
    StreamPath,
    WatchProbe,
    WatchSource,
)
from boba.canvas.journal import JournalWindow, StreamKey
from boba.chainlit.canvas.panel import CanvasPanel, StreamActions
from boba.chainlit.rendering.chat_view import (
    ChatSink,
    ChatView,
    RecordingSink,
    StepRole,
)
from boba.identity.context import CallContext, CallContexts
from boba.identity.run import Runs
from boba.mcp_client.client import DroppedSignals, McpServers, NamedBlocks
from boba.runtime import providers as runtime
from boba.runtime.di import Container
from boba.runtime.journal import DirVault, StreamJournal
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.chain import CallAmbient
from boba.toolkit.channels import CallOutcome, ToolChannel, WrapChannel
from boba.toolkit.dag import DagNode
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.result import MarkdownResult
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import DirectCalls, HostedTool, ToolHosting
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.streams import CallJournals, JournalWatchSource, ToolStream

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
USER = ServiceProcess.OWNER
"""Пользователь тестов — тот, под кем сервис стенда держит журналы."""
CALL_ID = "call-stream-1"

REMOTE = RemoteStand()
"""Журналы вызовов читаются с сервиса boba-mcp стенда: журнал теста лежит в
его каталоге журналов, адрес журнала хранит история треда."""
TOOL_NAME = "fake_bash"


def stream_path(call_id: str, channel: ToolChannel = ToolChannel.STDOUT) -> str:
    """Путь показа канала журнала в панели."""
    return StreamPath(call_id=call_id, channel=channel).render()


STREAM_PATH = stream_path(CALL_ID)


class TurnScope:
    """Запуск области теста: живые стримы регистрируются в нём и гаснут с ним."""

    def __init__(self, runs: Runs, context: CallContext) -> None:
        self._runs = runs
        self._context = context
        self._opened = ExitStack()

    def start(self) -> None:
        self._opened.enter_context(self._runs.open(self._context, FakeTurn()))

    def end(self) -> None:
        """Конец хода: запуск закрывается, файлы журнала остаются на диске."""
        self._opened.close()


@pytest.fixture(scope="module")
def journal_service(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ServiceProcess]:
    """Сервис boba-mcp стенда: панель читает журналы вызовов с него."""
    process = ServiceProcess(tmp_path_factory.mktemp("boba-mcp"))
    try:
        process.await_listening()
        yield process
    finally:
        process.stop()


@pytest.fixture
def journals(
    runtime_stand: StandRefs, di_root: None, journal_service: ServiceProcess
) -> Iterator[CallJournals]:
    """Журналы вызовов в каталоге журналов сервиса: так их пишет сервис,
    исполняя вызов; панель читает их с него. Запуск вызова — сам тред.
    Обвязка журнала инструмента берёт их из корневого контейнера."""
    store = StreamJournal(DirVault(str(journal_service.journal_dir)), reserve_bytes=0)
    built = CallJournals(store, runtime_stand.runs)
    root = Container.root
    if root is None:
        raise AssertionError("di_root installs the root container")

    root.provide(runtime.call_journals, built)
    REMOTE.forget()
    REMOTE.serve(journal_service)
    REMOTE.recorded(THREAD, CALL_ID)
    yield built
    shutil.rmtree(journal_service.journal_dir / USER, ignore_errors=True)


@pytest.fixture
def turn_scope(runtime_stand: StandRefs, call_stand: CallStand) -> Iterator[TurnScope]:
    scope = TurnScope(
        runtime_stand.runs, call_stand.context(THREAD, user_id=UUID(USER))
    )
    scope.start()
    yield scope
    scope.end()


@pytest.fixture(autouse=True)
def chainlit_context(
    call_stand: CallStand, journals: CallJournals, turn_scope: TurnScope
) -> Any:
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
    call_stand.use(call_stand.context(thread_id=THREAD, user_id=UUID(USER)))
    yield
    CanvasWatch.reset()
    # контекст сбрасывается за собой: иначе сессия утечёт в тесты без неё
    context_var.reset(token)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def begin_stream(journals: CallJournals, call_id: str = CALL_ID) -> ToolStream:
    journals.mark_streamable([TOOL_NAME])
    stream = journals.begin(USER, THREAD, call_id, TOOL_NAME)
    if stream is None:
        raise AssertionError("stream is not None")
    return stream


class TestJournalThroughWrapper:
    """Журнал вызова открывает обвязка и доводит тап до функции инструмента.

    Инструмент вызывается как его зовёт исполнитель — call с узлом вызова:
    call_id приезжает синтетическим полем схемы, sync-функция едет в
    executor-поток, где тап обязан отдать приёмник именно этого вызова.
    """

    def _tool_and_seen(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> tuple[HostedTool, list[object]]:
        seen: list[object] = []

        @tool
        def fake_bash(command: str) -> MarkdownResult:
            """Пишет в журнал то, что видит в тапе."""
            sinks = call_ambient.sinks()
            seen.append(sinks)
            if sinks is not None:
                sinks.sink_of(STDOUT).feed(f"ran: {command}".encode())
            return MarkdownResult(text="done")

        return self._logged(fake_bash, call_ambient, journals, call_stand), seen

    @staticmethod
    def _logged(
        payload: PayloadTool,
        call_ambient: CallAmbient,
        journals: CallJournals,
        call_stand: CallStand,
    ) -> HostedTool:
        """Инструмент хоста над телом payload под обвязкой журнала."""
        hosted = ToolHosting().hosted(payload)
        CallFields().attach_all([hosted])
        ToolRunLogger(journals, call_stand.contexts, call_ambient).guard_all([hosted])

        return hosted

    async def _invoke(
        self,
        call_ambient: CallAmbient,
        journals: CallJournals,
        call_stand: CallStand,
        *,
        streamable: bool = True,
    ) -> list[object]:
        if streamable:
            journals.mark_streamable([TOOL_NAME])

        fake_bash, seen = self._tool_and_seen(call_ambient, journals, call_stand)
        await fake_bash.call(
            DagNode(key=CALL_ID, tool=TOOL_NAME, args={"command": "echo hi"})
        )
        return seen

    def test_sync_tool_sees_its_recorder(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> None:
        seen = run(self._invoke(call_ambient, journals, call_stand))

        if len(seen) != 1:
            raise AssertionError("len(seen) == 1")
        if seen[0] is None:
            raise AssertionError("seen[0] is not None")

    def test_tool_output_lands_in_the_journal(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> None:
        run(self._invoke(call_ambient, journals, call_stand))

        piece = journals.recorded_slice(USER, THREAD, CALL_ID, offset=0, channel=STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if "ran: echo hi" not in piece.text:
            raise AssertionError('"ran: echo hi" in piece.text')

    def test_journal_is_closed_after_the_call(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> None:
        run(self._invoke(call_ambient, journals, call_stand))

        piece = journals.recorded_slice(USER, THREAD, CALL_ID, offset=0, channel=STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if piece.note != str(CallOutcome.FINISHED):
            raise AssertionError("piece.note == str(CallOutcome.FINISHED)")

    def test_not_streamable_tool_gets_no_recorder(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> None:
        seen = run(self._invoke(call_ambient, journals, call_stand, streamable=False))

        if seen != [None]:
            raise AssertionError("seen == [None]")
        if journals.live(THREAD, CALL_ID) is not None:
            raise AssertionError("journals.live(THREAD, CALL_ID) is None")

    def test_failed_call_closes_with_failure_note(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> None:
        journals.mark_streamable([TOOL_NAME])

        @tool
        def fake_bash(command: str) -> MarkdownResult:
            """Падает после записи в журнал."""
            sinks = call_ambient.sinks()
            if sinks is None:
                raise AssertionError("sinks is not None")
            sinks.sink_of(STDOUT).feed(b"partial")
            msg = "boom"
            raise RuntimeError(msg)

        hosted = self._logged(fake_bash, call_ambient, journals, call_stand)

        async def scenario() -> None:
            with pytest.raises(RuntimeError):
                await hosted.call(
                    DagNode(key=CALL_ID, tool=TOOL_NAME, args={"command": "x"})
                )

        run(scenario())

        piece = journals.recorded_slice(USER, THREAD, CALL_ID, offset=0, channel=STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if piece.note != str(CallOutcome.FAILED):
            raise AssertionError("piece.note == str(CallOutcome.FAILED)")
        if piece.text != "partial":
            raise AssertionError('piece.text == "partial"')

    def test_parallel_same_name_calls_keep_own_journals(
        self, call_ambient: CallAmbient, journals: CallJournals, call_stand: CallStand
    ) -> None:
        """Два одноимённых вызова: каждый пишет в файл своего call_id."""
        journals.mark_streamable([TOOL_NAME])

        @tool
        def fake_bash(command: str) -> MarkdownResult:
            """Пишет свою команду в свой журнал."""
            sinks = call_ambient.sinks()
            if sinks is None:
                raise AssertionError("sinks is not None")
            sinks.sink_of(STDOUT).feed(f"cmd: {command}".encode())
            return MarkdownResult(text="done")

        hosted = self._logged(fake_bash, call_ambient, journals, call_stand)

        async def scenario() -> None:
            first = hosted.call(
                DagNode(key="call-a", tool=TOOL_NAME, args={"command": "alpha"})
            )
            second = hosted.call(
                DagNode(key="call-b", tool=TOOL_NAME, args={"command": "beta"})
            )
            await asyncio.gather(first, second)

        run(scenario())

        alpha = journals.recorded_slice(
            USER, THREAD, "call-a", offset=0, channel=STDOUT
        )
        beta = journals.recorded_slice(USER, THREAD, "call-b", offset=0, channel=STDOUT)
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

    def test_slice_after_the_turn_ends(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed("прошлый ход".encode())
        stream.close(str(CallOutcome.FINISHED))
        turn_scope.end()

        if journals.live(THREAD, CALL_ID) is not None:
            raise AssertionError("journals.live(THREAD, CALL_ID) is None")

        piece = journals.recorded_slice(USER, THREAD, CALL_ID, offset=0, channel=STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.text != "прошлый ход":
            raise AssertionError('piece.text == "прошлый ход"')
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")

    def test_turn_end_closes_abandoned_recorder(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"data")

        turn_scope.end()

        piece = journals.recorded_slice(USER, THREAD, CALL_ID, offset=0, channel=STDOUT)
        if piece is None:
            raise AssertionError("piece is not None")
        if piece.closed is not True:
            raise AssertionError("piece.closed is True")
        if piece.note != CallOutcome.STOPPED.value:
            raise AssertionError("piece.note == CallOutcome.STOPPED.value")

    def test_foreign_user_cannot_read_the_journal(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"secret")
        turn_scope.end()

        piece = journals.recorded_slice(
            "999", THREAD, CALL_ID, offset=0, channel=STDOUT
        )
        if piece is not None:
            raise AssertionError("piece is None")


class TestBegin:
    """Регистрация живого вызова отвергает небезопасные идентификаторы."""

    def test_unsafe_call_id_is_refused(self, journals: CallJournals) -> None:
        journals.mark_streamable([TOOL_NAME])

        stream = journals.begin(USER, THREAD, "../../etc/passwd", TOOL_NAME)

        if stream is not None:
            raise AssertionError("stream is None")

    def test_dotted_call_id_is_refused(self, journals: CallJournals) -> None:
        journals.mark_streamable([TOOL_NAME])

        stream = journals.begin(USER, THREAD, "call.0", TOOL_NAME)

        if stream is not None:
            raise AssertionError("stream is None")


class FakeTransport(SignalTransport):
    """Транспорт в тестах: копит payload'ы сигналов вместо сокетов."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.dead = False

    def alive(self, thread_id: str) -> bool:
        return not self.dead

    async def send(self, thread_id: str, signal: CanvasSignal) -> None:
        self.sent.append(signal.payload())


def _speed_up_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CanvasWatch, "POLL_SEC", 0.05)
    monkeypatch.setattr(CanvasWatch, "COALESCE_SEC", 0.05)


def _journal_source(
    journals: CallJournals, call_id: str, live: ToolStream | None
) -> JournalWatchSource:
    journal = journals.store
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
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[FakeTransport, int]:
        _speed_up_watch(monkeypatch)

        async def scenario() -> tuple[FakeTransport, int]:
            transport = FakeTransport()
            CanvasWatch.configure(transport)

            stream = begin_stream(journals)
            source = _journal_source(journals, CALL_ID, stream)
            CanvasWatch.show(THREAD, STREAM_PATH, "n-1", source, seen="0:0")

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
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport, total = self._watched_writes(journals, monkeypatch)

        if not transport.sent:
            raise AssertionError("transport.sent")
        for payload in transport.sent:
            if payload["type"] != CanvasSignal.TYPE:
                raise AssertionError('payload["type"] == CanvasSignal.TYPE')
            if payload["path"] != STREAM_PATH:
                raise AssertionError('payload["path"] == STREAM_PATH')
            if "text" in payload:
                raise AssertionError('"text" not in payload')

        final = transport.sent[-1]
        if final["closed"] is not True:
            raise AssertionError('final["closed"] is True')
        if final["size"] != total:
            raise AssertionError('final["size"] == total')
        if final["note"] != str(CallOutcome.FINISHED):
            raise AssertionError('final["note"] == str(CallOutcome.FINISHED)')

    def test_signals_are_coalesced(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport, _ = self._watched_writes(journals, monkeypatch)

        if len(transport.sent) >= self.CHUNKS / 10:
            raise AssertionError("len(transport.sent) < self.CHUNKS / 10")

    def test_show_replaces_previous_watch(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            first = begin_stream(journals, "call-a")
            second = begin_stream(journals, "call-b")

            CanvasWatch.show(
                THREAD,
                stream_path("call-a"),
                "n-a",
                _journal_source(journals, "call-a", first),
            )
            CanvasWatch.show(
                THREAD,
                stream_path("call-b"),
                "n-b",
                _journal_source(journals, "call-b", second),
            )

            watching = CanvasWatch.watching(THREAD)
            CanvasWatch.drop(THREAD)
            return watching

        watching = run(scenario())
        if watching != stream_path("call-b"):
            raise AssertionError('watching == stream_path("call-b")')

    def test_leave_respects_the_nonce(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Гонка переоткрытия: leave старого показа не снимает свежий вотчер."""
        _speed_up_watch(monkeypatch)

        async def scenario() -> tuple[str | None, str | None]:
            CanvasWatch.configure(FakeTransport())
            stream = begin_stream(journals)
            CanvasWatch.show(
                THREAD,
                STREAM_PATH,
                "n-new",
                _journal_source(journals, CALL_ID, stream),
            )

            CanvasWatch.leave(THREAD, "n-old")
            after_foreign = CanvasWatch.watching(THREAD)

            CanvasWatch.leave(THREAD, "n-new")
            after_own = CanvasWatch.watching(THREAD)
            return after_foreign, after_own

        after_foreign, after_own = run(scenario())
        if after_foreign != STREAM_PATH:
            raise AssertionError("after_foreign == STREAM_PATH")
        if after_own is not None:
            raise AssertionError("after_own is None")

    def test_watch_stops_when_the_room_dies(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)

        async def scenario() -> None:
            transport = FakeTransport()
            CanvasWatch.configure(transport)
            stream = begin_stream(journals)
            CanvasWatch.show(
                THREAD,
                STREAM_PATH,
                "n-1",
                _journal_source(journals, CALL_ID, stream),
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

        class MutableFile(WatchSource):
            def __init__(self) -> None:
                self.revision = "r1"

            async def probe(self) -> WatchProbe:
                return WatchProbe(
                    revision=self.revision, size=1, closed=True, final=False
                )

            def attach_waker(self) -> None:
                return None

            def detach_waker(self, event: asyncio.Event) -> None:
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
        self,
        journals: CallJournals,
        turn_scope: TurnScope,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Закрытый вызов статичен: слежение снимается без единого сигнала."""
        _speed_up_watch(monkeypatch)

        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"done output")
        stream.close(str(CallOutcome.FINISHED))
        turn_scope.end()

        async def scenario() -> FakeTransport:
            transport = FakeTransport()
            CanvasWatch.configure(transport)
            CanvasWatch.show(
                THREAD,
                STREAM_PATH,
                "n-1",
                _journal_source(journals, CALL_ID, None),
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

    PATH = STREAM_PATH

    def _recorded(self, journals: CallJournals, turn_scope: TurnScope) -> None:
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(self.BODY)
        stream.close(str(CallOutcome.FINISHED))
        turn_scope.end()

    def test_windows_walk_the_journal(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded(journals, turn_scope)

        first = run(
            StreamActions.window(
                USER, THREAD, {"path": self.PATH, "offset": 0}, REMOTE.streams
            )
        )
        middle = run(
            StreamActions.window(
                USER, THREAD, {"path": self.PATH, "offset": 70000}, REMOTE.streams
            )
        )

        if first["stream"]["offset"] != 0:
            raise AssertionError('first["stream"]["offset"] == 0')
        if first["stream"]["size"] != len(self.BODY):
            raise AssertionError('first["stream"]["size"] == len(self.BODY)')
        if middle["stream"]["offset"] != 70000:
            raise AssertionError('middle["stream"]["offset"] == 70000')
        if len(middle["text"].encode()) != first["stream"]["window"]:
            raise AssertionError('len(middle["text"].encode()) == first["stream"]["wi…')

    def test_tail_window_by_negative_offset(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded(journals, turn_scope)

        tail = run(
            StreamActions.window(
                USER, THREAD, {"path": self.PATH, "offset": -1}, REMOTE.streams
            )
        )

        if tail["stream"]["end"] != len(self.BODY):
            raise AssertionError('tail["stream"]["end"] == len(self.BODY)')
        if len(tail["text"].encode()) > JournalWindow.BYTES:
            raise AssertionError('len(tail["text"].encode()) <= JournalWindow.BYTES')

    def test_window_before_joins_backwards(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded(journals, turn_scope)

        before = run(
            StreamActions.window(
                USER, THREAD, {"path": self.PATH, "before": 70000}, REMOTE.streams
            )
        )

        if before["stream"]["end"] != 70000:
            raise AssertionError('before["stream"]["end"] == 70000')

    def test_offset_beyond_the_file_gives_empty_window(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded(journals, turn_scope)

        beyond = run(
            StreamActions.window(
                USER, THREAD, {"path": self.PATH, "offset": 10**9}, REMOTE.streams
            )
        )

        if beyond["text"] != "":
            raise AssertionError('beyond["text"] == ""')
        if beyond["stream"]["offset"] != len(self.BODY):
            raise AssertionError('beyond["stream"]["offset"] == len(self.BODY)')

    def test_unknown_call_gives_empty_answer(self) -> None:
        answer = run(
            StreamActions.window(
                USER,
                THREAD,
                {"path": stream_path("no-such-call"), "offset": 0},
                REMOTE.streams,
            )
        )

        if answer != {}:
            raise AssertionError("answer == {}")


class TestChannelAccess:
    """Наружу читаются только stdout и stderr тела: остальное закрыто."""

    WRAP_PATH = f"{StreamPath.SCHEME}{CALL_ID}/{WrapChannel.STDERR.value}"

    @staticmethod
    def _recorded_with_wrap(journals: CallJournals, turn_scope: TurnScope) -> None:
        """Вызов, у которого писались и тело, и обвязка запуска."""
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"tool output")
        stream.sink_of(WrapChannel.STDERR).feed(b"image mounted")
        stream.close(str(CallOutcome.FINISHED))
        turn_scope.end()

    def test_wrap_channel_window_is_refused(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded_with_wrap(journals, turn_scope)

        answer = run(
            StreamActions.window(
                USER, THREAD, {"path": self.WRAP_PATH, "offset": 0}, REMOTE.streams
            )
        )

        if answer != {}:
            raise AssertionError("answer == {}")

    def test_wrap_channel_show_is_refused(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded_with_wrap(journals, turn_scope)

        try:
            run(
                StreamActions.show(
                    USER,
                    THREAD,
                    {"call_id": CALL_ID, "channel": WrapChannel.STDERR.value},
                    REMOTE.streams,
                )
            )
        except ValidationError:
            return

        raise AssertionError("StreamActions.show отвергает закрытый канал")

    def test_wrap_channel_is_written_but_not_offered_as_a_tab(
        self, turn_scope: TurnScope, journals: CallJournals
    ) -> None:
        self._recorded_with_wrap(journals, turn_scope)

        journal = journals.store
        if journal is None:
            raise AssertionError("journal is not None")

        key = StreamKey(user_id=USER, thread_id=THREAD, call_id=CALL_ID)
        if WrapChannel.STDERR not in journal.channels_of(key):
            raise AssertionError("WrapChannel.STDERR in journal.channels_of(key)")

        offered = journals.recorded_channels(USER, THREAD, CALL_ID)
        if WrapChannel.STDERR in offered:
            raise AssertionError("WrapChannel.STDERR not in offered")
        if STDOUT not in offered:
            raise AssertionError("STDOUT in offered")


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
        self,
        journals: CallJournals,
        turn_scope: TurnScope,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed("сохранённый вывод".encode())
        stream.close(str(CallOutcome.FINISHED))
        turn_scope.end()

        probe = PanelProbe(monkeypatch)

        run(StreamActions.show(USER, THREAD, {"call_id": CALL_ID}, REMOTE.streams))

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

    def test_inline_show_answers_instead_of_pushing(
        self,
        journals: CallJournals,
        turn_scope: TurnScope,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Вкладка канала в открытой панели: элемент не пушится, панель цела."""
        _speed_up_watch(monkeypatch)
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"tool output")
        stream.sink_of(ToolChannel.STDERR).feed(b"tool complains")
        stream.close(str(CallOutcome.FINISHED))
        turn_scope.end()

        probe = PanelProbe(monkeypatch)

        async def scenario() -> dict[str, Any]:
            CanvasWatch.configure(FakeTransport())
            answer = await StreamActions.show(
                USER,
                THREAD,
                {
                    "call_id": CALL_ID,
                    "channel": ToolChannel.STDERR.value,
                    "inline": True,
                },
                REMOTE.streams,
            )
            CanvasWatch.drop(THREAD)
            return answer

        answer = run(scenario())

        if probe.shown:
            raise AssertionError("not probe.shown")
        if answer["path"] != stream_path(CALL_ID, ToolChannel.STDERR):
            raise AssertionError(
                'answer["path"] == stream_path(CALL_ID, ToolChannel.STDERR)'
            )
        if "tool complains" not in answer["text"]:
            raise AssertionError('"tool complains" in answer["text"]')
        if not answer["nonce"]:
            raise AssertionError('answer["nonce"]')

    def test_inline_show_moves_the_watch_to_the_channel(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Слежение переезжает на выбранный канал: сигналы идут о нём."""
        _speed_up_watch(monkeypatch)
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"live")
        stream.sink_of(ToolChannel.STDERR).feed(b"live complaints")
        PanelProbe(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            await StreamActions.show(USER, THREAD, {"call_id": CALL_ID}, REMOTE.streams)
            await StreamActions.show(
                USER,
                THREAD,
                {
                    "call_id": CALL_ID,
                    "channel": ToolChannel.STDERR.value,
                    "inline": True,
                },
                REMOTE.streams,
            )
            watching = CanvasWatch.watching(THREAD)
            CanvasWatch.drop(THREAD)
            return watching

        watching = run(scenario())
        if watching != stream_path(CALL_ID, ToolChannel.STDERR):
            raise AssertionError("watching == stream_path(CALL_ID, ToolChannel.STDERR)")

    def test_show_registers_the_watch(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"live")

        probe = PanelProbe(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            await StreamActions.show(USER, THREAD, {"call_id": CALL_ID}, REMOTE.streams)
            watching = CanvasWatch.watching(THREAD)
            CanvasWatch.drop(THREAD)
            return watching

        watching = run(scenario())
        if watching != STREAM_PATH:
            raise AssertionError("watching == STREAM_PATH")
        if len(probe.shown) != 1:
            raise AssertionError("len(probe.shown) == 1")

    def test_unknown_stream_is_explained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = PanelProbe(monkeypatch)

        run(StreamActions.show(USER, THREAD, {"call_id": "no-such"}, REMOTE.streams))

        if probe.shown[0].kind is not CanvasKind.NOTICE:
            raise AssertionError("probe.shown[0].kind is CanvasKind.NOTICE")
        if "unavailable" not in probe.shown[0].note:
            raise AssertionError('"unavailable" in probe.shown[0].note')

    def test_leave_action_drops_the_watch(
        self, journals: CallJournals, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _speed_up_watch(monkeypatch)
        stream = begin_stream(journals)
        stream.sink_of(STDOUT).feed(b"live")

        probe = PanelProbe(monkeypatch)

        async def scenario() -> str | None:
            CanvasWatch.configure(FakeTransport())
            await StreamActions.show(USER, THREAD, {"call_id": CALL_ID}, REMOTE.streams)
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

    async def drop(self, step: Step) -> None:
        kept: list[Step] = []
        for stored in self.steps:
            if stored.id == step.id:
                continue

            kept.append(stored)

        self.steps = kept

    def rebuilt(self) -> None:
        self.steps.clear()


class TestStreamButton:
    """Кнопка потока живёт на шаге потокового тула и адресуется по call_id."""

    async def _tool_step(
        self,
        journals: CallJournals,
        sink: ChatSink,
        name: str,
    ) -> Step:
        view = ChatView(THREAD, sink, journals, user_name="tester")
        view.begin_turn("turn-1")
        return await view.tool_started(name, {"command": "ls"}, CALL_ID)

    def test_streamable_tool_gets_the_button(
        self, journals: CallJournals, runtime_stand: StandRefs
    ) -> None:
        journals.mark_streamable([TOOL_NAME])
        sink = ElementSink()

        step = run(self._tool_step(journals, sink, TOOL_NAME))

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

    def test_no_journal_means_no_button(self, runtime_stand: StandRefs) -> None:
        silent = CallJournals(None, runtime_stand.runs)
        silent.mark_streamable([TOOL_NAME])

        step = run(self._tool_step(silent, ElementSink(), TOOL_NAME))

        if step.elements:
            raise AssertionError("not step.elements")

    def test_other_tools_stay_clean(
        self, journals: CallJournals, runtime_stand: StandRefs
    ) -> None:
        sink = ElementSink()

        step = run(self._tool_step(journals, sink, "diagram_save"))

        if step.elements:
            raise AssertionError("not step.elements")

    def test_replay_sink_never_emits_the_button(
        self, journals: CallJournals, runtime_stand: StandRefs
    ) -> None:
        journals.mark_streamable([TOOL_NAME])

        step = run(self._tool_step(journals, RecordingSink(), TOOL_NAME))

        if step.elements:
            raise AssertionError("not step.elements")

    def test_replayed_step_dict_matches_live(
        self, journals: CallJournals, runtime_stand: StandRefs
    ) -> None:
        """Кнопка не должна ломать контракт шагов: сравниваются StepDict."""
        journals.mark_streamable([TOOL_NAME])

        live = run(self._tool_step(journals, ElementSink(), TOOL_NAME)).to_dict()
        replay = run(self._tool_step(journals, RecordingSink(), TOOL_NAME)).to_dict()

        if live["id"] != replay["id"]:
            raise AssertionError('live["id"] == replay["id"]')
        if live["name"] != replay["name"]:
            raise AssertionError('live["name"] == replay["name"]')
        if live["parentId"] != replay["parentId"]:
            raise AssertionError('live["parentId"] == replay["parentId"]')


class TestStreamDownload:
    """Скачивание журнала вызова: чат передаёт ответ сервера, который
    исполнял вызов, как есть."""

    @staticmethod
    def _app(servers: McpServers) -> Any:
        from chainlit.auth import get_current_user
        from chainlit.user import PersistedUser
        from fastapi import FastAPI

        from boba.chainlit.data.upload import StreamServing
        from boba.chainlit.domain.keys import StreamUrl

        serving = StreamServing(lambda: REMOTE.journals, lambda: servers)

        app = FastAPI()
        app.add_api_route(StreamUrl.ROUTE, serving.serve, methods=["GET"])
        user = PersistedUser(
            id=USER, identifier="tester", createdAt="2024-01-01T00:00:00Z"
        )
        app.dependency_overrides[get_current_user] = lambda: user
        return app

    def test_log_downloads_whole_and_by_range(
        self, journals: CallJournals, journal_service: ServiceProcess
    ) -> None:
        stream = begin_stream(journals)
        body = "строка вывода\n" * 20
        stream.sink_of(STDOUT).feed(body.encode())
        stream.close(str(CallOutcome.FINISHED))

        from httpx import ASGITransport, AsyncClient

        async def scenario() -> Any:
            servers = McpServers(
                journal_service.token_servers(REMOTE.SERVER),
                NamedBlocks(),
                DroppedSignals(),
                CallContexts(),
                DirectCalls(),
            )
            transport = ASGITransport(app=self._app(servers))
            try:
                async with AsyncClient(
                    transport=transport, base_url="https://t"
                ) as client:
                    whole = await client.get(f"/stream/{THREAD}/{CALL_ID}")
                    part = await client.get(
                        f"/stream/{THREAD}/{CALL_ID}", headers={"Range": "bytes=0-9"}
                    )
                    missing = await client.get(f"/stream/{THREAD}/absent-call")
            finally:
                await servers.stop()

            return whole, part, missing

        whole, part, missing = run(scenario())

        if whole.status_code != 200:
            raise AssertionError(f"the journal is served whole: {whole.status_code}")
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
