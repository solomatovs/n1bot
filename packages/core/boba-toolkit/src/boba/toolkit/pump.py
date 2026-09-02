"""Общая механика исполнения вызова: вход в тело, насос чтения, прогон.

Обе реализации ToolLauncher — субпроцесс (boba.toolrun.process) и песочница
(boba.sandbox.zygote) — исполняют вызов одинаково и различаются только
устройством процесса и каналов. Общее собрано здесь:

- CallInput / FrameInput — единственное горлышко записи в тело: прямая
  блокирующая запись в пайп из потока вызывающего. Полный буфер пайпа
  останавливает запись, и скорость входа прижимается к скорости тела —
  это backpressure без очередей в памяти хоста.
- ChannelPump — базовый насос чтения каналов тела (select, дедлайн,
  отмена, добивание); реализации наследуют его.
- OpenRun — открытый прогон вызова: насос крутится своим потоком, вход
  остаётся у вызывающего; PumpedCall наследует его и добавляет контракт
  ToolCall для потоковых инструментов.
- CallSinks / Tee — сборка приёмников каналов вместе с журналом вызова.

Ошибки:
LauncherError — вход вызова уже закрыт, у кадров уже есть читатель либо
    насос не оставил итога.
ToolStopped — вызов остановлен отменой хода либо close().
"""

from __future__ import annotations

import os
import selectors
import threading
import time
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from boba.cancellation import RunCancellation, current_cancellation
from boba.toolkit.channels import ToolChannel
from boba.toolkit.frames import (
    CallInbox,
    FrameCodec,
    FrameLimit,
    ToolFrame,
)
from boba.toolkit.launcher import LauncherError, ToolCall, ToolOutcome
from boba.toolkit.stream import Chunk, ChunkSink, ToolChannelsTap

__all__ = [
    "CallInput",
    "CallSinks",
    "ChannelPump",
    "FrameInput",
    "JournaledFrameInput",
    "OpenRun",
    "PumpEnd",
    "PumpedCall",
    "Tee",
]


class Tee:
    """Тройник: одна порция канала уходит в два приёмника сразу.

    Нужен, когда канал читают и свой буфер вызова, и журнал (CallSinks).
    """

    def __init__(self, first: ChunkSink, second: ChunkSink) -> None:
        self._first = first
        self._second = second

    def feed(self, chunk: Chunk) -> None:
        self._first(chunk)
        self._second(chunk)


class CallSinks:
    """Собирает приёмники каналов вызова: свои буферы плюс журнал.

    Журнал вызова обвязка передаёт через contextvar (ToolChannelsTap);
    здесь его приёмники подключаются тройником (Tee) к своим, а каналы без
    своего приёмника пишутся только в журнал. Зовётся в потоке вызывающего:
    в поток насоса contextvar не переезжает, и журнал там уже не найти.
    """

    @staticmethod
    def stdin_input(fd: int) -> FrameInput:
        """Вход кадров вызова: с журналом входных заголовков, когда тап
        поставлен; без журнала — обычный FrameInput."""
        journal = ToolChannelsTap.get()
        if journal is None:
            return FrameInput(fd)

        return JournaledFrameInput(fd, journal.sink_of(ToolChannel.STDIN).feed)

    @staticmethod
    def merged(
        own: Mapping[ToolChannel, ChunkSink],
        journal_channels: Sequence[ToolChannel],
    ) -> dict[ToolChannel, ChunkSink]:
        sinks: dict[ToolChannel, ChunkSink] = dict(own)

        journal = ToolChannelsTap.get()
        if journal is None:
            return sinks

        for channel in journal_channels:
            journal_sink = journal.sink_of(channel).feed

            mine = sinks.get(channel)
            if mine is None:
                sinks[channel] = journal_sink
            else:
                sinks[channel] = Tee(mine, journal_sink).feed

        return sinks


class CallInput:
    """Вход вызова: прямая блокирующая запись в stdin-пайп тела.

    Единственный способ передать телу данные — так backpressure получается
    сам собой: пишет поток вызывающего, и когда тело не успевает читать,
    запись стоит на полном буфере пайпа. Разрыв пайпа (тело умерло или
    закрыло stdin) закрывает вход: запись, на которой это случилось, молчит
    — причину сбоя объяснит итог вызова кодом возврата и stderr, — а
    последующие send падают ошибкой закрытого входа.

    Базовый для FrameInput; сырым CallInput пишутся injected-конфиг и stdin
    shell-команды.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._lock = threading.Lock()
        self._open = True
        self._broken = False

    def send_bytes(self, data: bytes) -> None:
        """Байты входа телу; после finish, abandon или разрыва — LauncherError."""
        with self._lock:
            self._require_open()
            self._write(data)

    def finish(self) -> None:
        """Конец входа: EOF телу закрытием пайпа; повтор безвреден."""
        with self._lock:
            if not self._open:
                return

            self._close()

    def abandon(self) -> None:
        """Закрыть вход на пути отмены и уборки; повтор безвреден."""
        with self._lock:
            if not self._open:
                return

            self._close()

    def _require_open(self) -> None:
        if self._open:
            return

        if self._broken:
            msg = "call input is broken: the tool stopped reading it"
            raise LauncherError(msg)

        msg = "call input is already closed"
        raise LauncherError(msg)

    def _write(self, data: bytes) -> None:
        """Записать всё; разрыв пайпа закрывает вход молча (см. докстринг класса)."""
        view = memoryview(data)

        while view.nbytes:
            try:
                written = os.write(self._fd, view)
            except OSError:
                self._broken = True
                self._close()
                return

            view = view[written:]

    def _close(self) -> None:
        self._open = False

        try:
            os.close(self._fd)
        except OSError:
            return


class FrameInput(CallInput):
    """Вход вызова кадрами: send кодирует ToolFrame в байты и пишет их тем же
    блокирующим способом, что и базовый CallInput; finish даёт телу EOF
    закрытием пайпа."""

    def __init__(self, fd: int) -> None:
        super().__init__(fd)
        self._codec = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)

    def send(self, frame: ToolFrame) -> None:
        self.send_bytes(self._codec.encode(frame))


class JournaledFrameInput(FrameInput):
    """Наследник FrameInput, дублирующий отправляемые байты в журнал вызова.

    Журнальный приёмник канала tool_stdin пишет заголовки кадров (тела
    пропускает — FrameHeadsSink), поэтому по журналу видно, что хост слал
    телу. Создаётся через CallSinks.stdin_input, когда журнальный тап
    поставлен.
    """

    def __init__(self, fd: int, tap: ChunkSink) -> None:
        super().__init__(fd)
        self._tap = tap

    def send_bytes(self, data: bytes) -> None:
        self._tap(data)
        super().send_bytes(data)


@dataclass(frozen=True)
class PumpEnd:
    """Что насос знает о прогоне после его конца: был ли таймаут и когда
    пришёл первый байт вывода. Код возврата сюда не входит — его источник у
    каждой реализации свой (poll процесса, control-сокет зиготы)."""

    timed_out: bool
    first_output_ms: int | None


class ChannelPump:
    """Базовый насос чтения каналов вызова: select по дескрипторам, дедлайн,
    реакция на отмену и добивание исполнителя.

    Крутится в потоке прогона (OpenRun) и только читает: входом тела
    владеет CallInput в потоке вызывающего. Наследники — _ProcessPump в
    boba.toolrun.process и _ZygotePump в boba.sandbox.zygote — подставляют
    устройство процесса тремя методами: _finished (исполнитель завершился),
    _kill (добить) и, при нужде, _quit_on_timeout (прекратить ждать выхода
    после таймаута).
    """

    READ_BYTES: ClassVar[int] = 65536

    def __init__(self, poll_sec: float, timeout_sec: float) -> None:
        self._poll_sec = poll_sec
        self._timeout_sec = timeout_sec
        self._selector = selectors.DefaultSelector()
        self._sinks: dict[int, ChunkSink] = {}
        self._events: dict[int, Callable[[], None]] = {}
        self._open_reads: set[int] = set()
        self._timed_out = False
        self._first_output: float | None = None
        self._started = 0.0

    def add_read(self, fd: int, sink: ChunkSink) -> None:
        """Канал данных: читается порциями до EOF, порции идут в приёмник."""
        os.set_blocking(fd, False)
        self._selector.register(fd, selectors.EVENT_READ)
        self._sinks[fd] = sink
        self._open_reads.add(fd)

    def add_drain(self, fd: int) -> None:
        """Канал без потребителя: дочитывается в никуда, чтобы тело не встало."""
        self.add_read(fd, self._swallow)

    def add_event(self, fd: int, handler: Callable[[], None]) -> None:
        """Слот событий: на готовности дескриптора зовётся обработчик."""
        self._selector.register(fd, selectors.EVENT_READ)
        self._events[fd] = handler

    def drop_event(self, fd: int) -> None:
        """Снять слот событий; обработчик зовёт это, когда событий больше не ждёт."""
        if fd not in self._events:
            return

        self._selector.unregister(fd)
        del self._events[fd]

    def run(self, cancellation: RunCancellation) -> PumpEnd:
        """Качать каналы до выхода исполнителя; следит за дедлайном и отменой.

        Селектор здесь не закрывается: путь срыва дочитывает каналы через
        abort. Владелец обязан позвать close на любом исходе.
        """
        self._started = time.monotonic()
        deadline = self._started + self._timeout_sec

        with cancellation.abort_with(self._kill):
            while self._open_reads or not self._finished():
                if cancellation.cancelled:
                    self._kill()

                if not self._timed_out and time.monotonic() >= deadline:
                    self._timed_out = True
                    self._kill()

                if self._timed_out and self._quit_on_timeout():
                    break

                self._step()

        cancellation.raise_if_cancelled()

        first_output_ms: int | None = None
        if self._first_output is not None:
            first_output_ms = int((self._first_output - self._started) * 1000)

        return PumpEnd(timed_out=self._timed_out, first_output_ms=first_output_ms)

    def abort(self, grace_sec: float) -> None:
        """Добить исполнителя и дочитать каналы впустую, чтобы он отпустил ресурсы.

        Путь срыва (приёмник поднял исключение): исход вызова уже решён,
        поэтому дальнейшие порции глотаются, а любая ошибка каналов означает,
        что исполнитель мёртв, — этого и ждали.
        """
        self._kill()
        self._mute()

        deadline = time.monotonic() + grace_sec

        while not self._finished() and time.monotonic() < deadline:
            try:
                self._step()
            except Exception:
                return

    def timed_out(self) -> bool:
        return self._timed_out

    def close(self) -> None:
        """Отпустить селектор; повторный вызов безвреден."""
        self._selector.close()

    @abstractmethod
    def _finished(self) -> bool:
        """Исполнитель завершился; каналы закрываются EOF независимо."""
        ...

    @abstractmethod
    def _kill(self) -> None:
        """Добить исполнителя; повторный вызов обязан быть безвредным."""
        ...

    def _quit_on_timeout(self) -> bool:
        """Прекратить ждать выхода после таймаута; по умолчанию ждём _finished."""
        return False

    def _mute(self) -> None:
        """Заменить приёмники на глотание: добивание не интересуется данными."""
        for fd in self._sinks:
            self._sinks[fd] = self._swallow

    @staticmethod
    def _swallow(_data: Chunk) -> None:
        """Приёмник без потребителя."""

    def _step(self) -> None:
        ready = self._selector.select(timeout=self._poll_sec)

        for key, _ in ready:
            handler = self._events.get(key.fd)
            if handler is not None:
                handler()
                continue

            self._read(key.fd)

    def _read(self, fd: int) -> None:
        chunk = os.read(fd, self.READ_BYTES)
        if not chunk:
            self._selector.unregister(fd)
            self._open_reads.discard(fd)
            return

        if self._first_output is None:
            self._first_output = time.monotonic()

        self._sinks[fd](chunk)


RunEnd = TypeVar("RunEnd")


class OpenRun(Generic[RunEnd]):
    """Открытый прогон вызова: насос читает каналы своим потоком, а вход
    пишет вызывающий через entry (CallInput).

    Базовый класс исполнения любого вызова; два потока — и есть решение:
    вызывающий может стоять на записи входа, пока насос читает вывод, и
    взаимной блокировки не случается. PumpedCall наследует его для
    потоковых инструментов; shell-команда пользуется напрямую — пишет весь
    stdin и ждёт итога wait().

    Прерыватель внешней отмены регистрируется в конструкторе, до старта
    потока насоса, поэтому отмена хода сразу после открытия не теряется.
    Уже отменённый ход роняет конструктор ToolStopped — тогда прибрать
    процесс и каналы обязан вызывающий: насос ещё не жил и добить некому.
    Функция run исполняется в потоке насоса и обязана на любом исходе
    добить процесс и закрыть host-концы каналов.
    """

    def __init__(
        self,
        tool: str,
        entry: CallInput,
        run: Callable[[RunCancellation], RunEnd],
    ) -> None:
        self._tool = tool
        self._entry = entry
        self._run = run
        self._own = RunCancellation()
        self._relay = ExitStack()
        self._end: RunEnd | None = None
        self._failure: BaseException | None = None

        outer = current_cancellation()
        self._relay.enter_context(outer.abort_with(self._own.cancel))

        self._worker = threading.Thread(
            target=self._pump_call,
            name=f"tool-call:{tool}",
            daemon=True,
        )
        self._worker.start()

    @property
    def entry(self) -> CallInput:
        return self._entry

    def wait(self) -> RunEnd:
        """Дождаться конца насоса и отдать итог; сбой прогона поднимается тут."""
        self._worker.join()
        self._settle()

        if self._failure is not None:
            raise self._failure

        end = self._end
        if end is None:
            msg = f"{self._tool}: call pump left no result"
            raise LauncherError(msg)

        return end

    def halt(self) -> None:
        """Добить прогон, не интересуясь итогом; повтор безвреден."""
        self._own.cancel()
        self._worker.join()
        self._settle()

    def _settle(self) -> None:
        """Снять прерыватель внешней отмены и прибрать вход; повтор безвреден."""
        self._relay.close()
        self._entry.abandon()

    def _pump_call(self) -> None:
        with self._own.published():
            try:
                self._end = self._run(self._own)
            except BaseException as exc:
                self._failure = exc
            finally:
                self._finalize()

    def _finalize(self) -> None:
        """Насос кончился; подкласс закрывает здесь своих читателей."""
        return


class PumpedCall(OpenRun[RunEnd], ToolCall):
    """Реализация протокола ToolCall поверх OpenRun: открытый потоковый
    вызов инструмента.

    Создаётся методом open() реализаций ToolLauncher. К прогону добавляет
    кадры: send и done_sending пишут вход через FrameInput, frames() отдаёт
    кадры тела из CallInbox (читатель ровно один), result() ждёт конца
    насоса и собирает ToolOutcome переданной функцией finish.
    """

    def __init__(
        self,
        tool: str,
        entry: FrameInput,
        inbox: CallInbox,
        run: Callable[[RunCancellation], RunEnd],
        finish: Callable[[RunEnd], ToolOutcome],
    ) -> None:
        self._frames = entry
        self._inbox = inbox
        self._finish = finish
        self._outcome: ToolOutcome | None = None
        self._frames_taken = False

        # поля читателей выставлены до конструктора низа: он стартует поток
        super().__init__(tool, entry, run)

    def send(self, frame: ToolFrame) -> None:
        self._frames.send(frame)

    def done_sending(self) -> None:
        self._frames.finish()

    def frames(self) -> Iterator[ToolFrame]:
        if self._frames_taken:
            msg = f"{self._tool}: call frames already have a reader"
            raise LauncherError(msg)

        self._frames_taken = True
        return self._inbox.frames()

    def result(self) -> ToolOutcome:
        if self._outcome is not None:
            return self._outcome

        end = self.wait()

        self._outcome = self._finish(end)
        return self._outcome

    def close(self) -> None:
        if self._outcome is not None:
            return

        self.halt()

    def _finalize(self) -> None:
        self._inbox.close()
