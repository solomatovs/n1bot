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
- CallInputs / HostGate — входы вызова по слотам и барьер группы: ответ
  телу на StreamGroup.ready().

Ошибки:
LauncherError — вход вызова уже закрыт, у кадров уже есть читатель либо
    насос не оставил итога.
ToolStopped — вызов остановлен отменой хода либо close().
"""

from __future__ import annotations

import fcntl
import os
import selectors
import threading
import time
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from boba.cancellation import RunCancellation, current_cancellation
from boba.toolkit.channels import ToolChannel
from boba.toolkit.frames import (
    CallInbox,
    FrameCodec,
    FrameLimit,
    PartsWriter,
    ToolFrame,
)
from boba.toolkit.launcher import (
    CallGate,
    CallInputPort,
    LauncherError,
    ToolCall,
    ToolOutcome,
)
from boba.toolkit.ports import GateSignal
from boba.toolkit.protocol import CallGateMode, CallInputSpec
from boba.toolkit.stream import Chunk, ChunkSink, ToolChannelsTap

__all__ = [
    "CallInput",
    "CallInputs",
    "CallSinks",
    "ChannelPump",
    "FrameInput",
    "HostGate",
    "JournaledFrameInput",
    "OpenRun",
    "PipePlumbing",
    "PumpEnd",
    "PumpedCall",
    "RawInput",
    "Tee",
]


class PipePlumbing:
    """Настройка пайпов каналов данных: просит у ядра буфер пошире.

    Чем больше буфер пайпа, тем реже просыпается насос и тем длиннее
    порции на тех же данных — дешёвое ускорение массивных перекачек.
    Отказ ядра (потолок /proc/sys/fs/pipe-max-size ниже запрошенного) не
    ошибка: остаётся дефолтный буфер, это оптимизация, а не контракт.
    """

    DATA_PIPE_BYTES: ClassVar[int] = 1 << 20

    @classmethod
    def widen(cls, fd: int) -> None:
        """Расширить буфер пайпа канала данных; действует на весь пайп."""
        try:
            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, cls.DATA_PIPE_BYTES)
        except OSError:
            return

    BUDGET_HINT: ClassVar[str] = (
        "the kernel refused the size: the per-user pipe buffer budget is "
        "likely exhausted (fs.pipe-user-pages-soft) or the size is above "
        "fs.pipe-max-size"
    )

    @classmethod
    def require(cls, fd: int, pipe_bytes: int, channel: str) -> None:
        """Поставить пайпу канала ровно запрошенный буфер — или отказать.

        Нулевой размер — требования нет: буфер расширяется мягко (widen).
        Размер задаёт писатель канала (pipe_bytes вызова), и меньший буфер
        молча замедлил бы перекачку — поэтому недодача ядра не глотается,
        а срывает запуск с подсказкой про ручку ядра.

        Ошибки:
        LauncherError — ядро не дало пайпу запрошенный размер.
        """
        if pipe_bytes <= 0:
            cls.widen(fd)
            return

        try:
            got = fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, pipe_bytes)
        except OSError as exc:
            msg = (
                f"channel {channel!r}: setting the pipe buffer to "
                f"{pipe_bytes} bytes failed: {exc}; {cls.BUDGET_HINT}"
            )
            raise LauncherError(msg) from exc

        if got < pipe_bytes:
            msg = (
                f"channel {channel!r}: asked for a {pipe_bytes}-byte pipe "
                f"buffer, the kernel gave {got}; {cls.BUDGET_HINT}"
            )
            raise LauncherError(msg)


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

    @classmethod
    def call_inputs(
        cls,
        stdin_fd: int,
        port_fds: Sequence[int],
        specs: Sequence[CallInputSpec],
    ) -> CallInputs:
        """Входы вызова по ToolCommand.inputs: у каждого свой пайп из
        port_fds по порядку. stdin процесса порт не несёт — это служебный
        канал (shell-команда, EOF телу без портов), его закрывает хост.
        Журнал заголовков ведёт только первый кадровый вход: канал
        tool_stdin в журнале один."""
        if len(port_fds) != len(specs):
            msg = (
                f"call inputs: {len(specs)} inputs need {len(specs)} pipes, "
                f"got {len(port_fds)}"
            )
            raise LauncherError(msg)

        slots: list[FrameInput] = []
        for index, (fd, spec) in enumerate(zip(port_fds, specs, strict=True)):
            slots.append(cls._input_of(fd, spec, journaled=index == 0))

        return CallInputs(FrameInput(stdin_fd), tuple(slots))

    @staticmethod
    def _input_of(fd: int, spec: CallInputSpec, *, journaled: bool) -> FrameInput:
        """Вход кадровый — с журналом заголовков, когда тап поставлен; сырой —
        голые байты без кадров и журнала."""
        if spec.raw:
            return RawInput(fd)

        if not journaled:
            return FrameInput(fd)

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
        self._writer = PartsWriter()
        self._open = True
        self._broken = False

    def send_bytes(self, data: Chunk) -> None:
        """Байты входа телу; после finish, abandon или разрыва — LauncherError."""
        with self._lock:
            self._require_open()
            self._write_parts((data,))

    def send_parts(self, parts: Sequence[Chunk]) -> None:
        """Несколько частей одной записью (writev): части кадра без склейки."""
        with self._lock:
            self._require_open()
            self._write_parts(parts)

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

    def take_fd(self) -> int:
        """Отдать дескриптор входа раздаче каналов группы (ChannelFanOut).

        Владение уходит вместе с дескриптором: закрывает его раздача,
        а send/finish на этом входе больше не работают.
        """
        with self._lock:
            self._require_open()
            self._open = False

            return self._fd

    def _require_open(self) -> None:
        if self._open:
            return

        if self._broken:
            msg = (
                f"call input fd {self._fd} is broken: the tool stopped reading "
                "it (the pipe closed on write), later sends are rejected"
            )
            raise LauncherError(msg)

        msg = (
            f"call input fd {self._fd} is already closed by finish/abandon, "
            "further sends are rejected"
        )
        raise LauncherError(msg)

    def _write_parts(self, parts: Sequence[Chunk]) -> None:
        """Записать все части writev'ом по порядку; разрыв пайпа закрывает
        вход молча (см. докстринг класса)."""
        try:
            self._writer.write(self._fd, parts)
        except OSError:
            self._broken = True
            self._close()

    def _close(self) -> None:
        self._open = False

        try:
            os.close(self._fd)
        except OSError:
            return


class FrameInput(CallInput, CallInputPort):
    """Реализация CallInputPort кадрами: send кодирует ToolFrame в байты и
    пишет их тем же блокирующим способом, что и базовый CallInput; finish
    даёт телу EOF закрытием пайпа."""

    def __init__(self, fd: int) -> None:
        super().__init__(fd)
        self._codec = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)

    def send(self, frame: ToolFrame) -> None:
        self.send_parts(self._codec.encode_parts(frame.header, frame.body))


class JournaledFrameInput(FrameInput):
    """Наследник FrameInput, дублирующий отправляемые байты в журнал вызова.

    Журнальный приёмник канала tool_stdin пишет заголовки кадров (тела
    пропускает — FrameHeadsSink), поэтому по журналу видно, что хост слал
    телу. Создаётся через CallSinks.call_inputs, когда журнальный тап
    поставлен.
    """

    def __init__(self, fd: int, tap: ChunkSink) -> None:
        super().__init__(fd)
        self._tap = tap

    def send_bytes(self, data: Chunk) -> None:
        self._tap(data)
        super().send_bytes(data)

    def send_parts(self, parts: Sequence[Chunk]) -> None:
        # журнальный кодек инкрементален: части скармливаются по очереди
        for part in parts:
            self._tap(part)

        super().send_parts(parts)


class RawInput(FrameInput):
    """Вход истинно сырого канала: по нему идут голые байты (send_bytes).

    Кадровый send запрещён — рамки кадра попали бы прямо в данные тела.
    Журнала у сырого входа нет: разбирать в нём нечего. Создаётся через
    CallSinks.call_inputs для входа RawInbound-порта.
    """

    def send(self, frame: ToolFrame) -> None:
        msg = (
            f"call input fd {self._fd} is a raw byte channel (RawInbound): "
            f"a frame with {len(frame.body)} body bytes is not accepted, "
            "use send_bytes"
        )
        raise LauncherError(msg)


class HostGate(CallGate):
    """Реализация CallGate на паре пайпов: gate от тела, verdict к телу.

    Пайп gate читает насос вызова (watch регистрирует его событием
    селектора), закрывают его каналы лончера вместе с прочими host-концами.
    Сигнал готовности тела в режиме AUTO сразу разрешается, в HELD уходит
    владельцу через claim. Ответ пишется один раз и закрывает verdict —
    повторный ответ ничего не делает; close закрывает verdict без ответа, и
    тело, ждущее барьера, увидит EOF как запрет. Создаётся лончером на
    каждый вызов.
    """

    READ_BYTES: ClassVar[int] = 64

    def __init__(self, verdict_fd: int, mode: CallGateMode) -> None:
        self._verdict_fd = verdict_fd
        self._mode = mode
        self._lock = threading.Lock()
        self._on_ready: Callable[[], None] = self.release
        self._claimed = False
        self._arrived = False
        self._answered = False

    def watch(self, pump: ChannelPump, gate_fd: int) -> None:
        """Отдать пайп gate насосу: сигналы тела читаются его потоком."""
        os.set_blocking(gate_fd, False)

        def readable() -> None:
            self._read(pump, gate_fd)

        pump.add_event(gate_fd, readable)

    def claim(self, on_ready: Callable[[], None]) -> None:
        with self._lock:
            self._on_ready = on_ready
            self._claimed = True
            fire = self._arrived

        if fire:
            on_ready()

    def release(self) -> None:
        self._answer(GateSignal.GO)

    def refuse(self) -> None:
        self._answer(GateSignal.ABORT)

    def close(self) -> None:
        """Конец вызова: verdict закрывается, если ответа не было."""
        with self._lock:
            if self._answered:
                return

            self._answered = True

        with suppress(OSError):
            os.close(self._verdict_fd)

    def _answer(self, signal: GateSignal) -> None:
        with self._lock:
            if self._answered:
                return

            self._answered = True

        # мёртвому телу ответ не нужен: причину объяснит итог вызова
        with suppress(BrokenPipeError):
            os.write(self._verdict_fd, signal.bytes())

        with suppress(OSError):
            os.close(self._verdict_fd)

    def _read(self, pump: ChannelPump, gate_fd: int) -> None:
        try:
            data = os.read(gate_fd, self.READ_BYTES)
        except BlockingIOError:
            return

        if not data:
            pump.drop_event(gate_fd)
            return

        self._arrive()

    def _arrive(self) -> None:
        with self._lock:
            self._arrived = True
            on_ready = self._on_ready
            waits_owner = self._mode is CallGateMode.HELD
            if self._claimed:
                waits_owner = False

        if waits_owner:
            return

        on_ready()


class CallInputs:
    """Входы открытого вызова: слоты по ToolCommand.inputs плюс stdin.

    Каждый слот едет своим пайпом, симметрично выходам. stdin процесса
    порта не несёт, но есть у любого вызова: тело модуля его не читает, а
    закрыть его обязан хост — поэтому stdin хранится отдельно от слотов и
    закрывается вместе с ними. Собирает CallSinks.call_inputs.
    """

    def __init__(self, stdin: FrameInput, slots: Sequence[FrameInput]) -> None:
        self._stdin = stdin
        self._slots = tuple(slots)

    def slots(self) -> tuple[FrameInput, ...]:
        return self._slots

    def entries(self) -> tuple[CallInput, ...]:
        """Все пайпы входа для уборки: stdin и слоты."""
        return (self._stdin, *self._slots)

    def finish_all(self) -> None:
        for entry in self.entries():
            entry.finish()

    def abandon_all(self) -> None:
        for entry in self.entries():
            entry.abandon()


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
    """Открытый прогон вызова: насос читает каналы своим потоком, а входы
    пишет вызывающий через entries (CallInput).

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
        entries: Sequence[CallInput],
        run: Callable[[RunCancellation], RunEnd],
    ) -> None:
        self._tool = tool
        self._entries = tuple(entries)
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

    def wait(self) -> RunEnd:
        """Дождаться конца насоса и отдать итог; сбой прогона поднимается тут."""
        self._worker.join()
        self._settle()

        if self._failure is not None:
            raise self._failure

        end = self._end
        if end is None:
            msg = (
                f"{self._tool}: call pump thread finished without a failure "
                "and without a run result"
            )
            raise LauncherError(msg)

        return end

    def halt(self) -> None:
        """Добить прогон, не интересуясь итогом; повтор безвреден."""
        self._own.cancel()
        self._worker.join()
        self._settle()

    def _settle(self) -> None:
        """Снять прерыватель внешней отмены и прибрать входы; повтор безвреден."""
        self._relay.close()
        for entry in self._entries:
            entry.abandon()

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
    кадры: inputs отдаёт входы вызова (CallInputs), frames() — кадры тела из
    CallInbox (читатель ровно один), result() ждёт конца насоса и собирает
    ToolOutcome переданной функцией finish.
    """

    def __init__(  # noqa: PLR0913 — фикстуры теста
        self,
        tool: str,
        inputs: CallInputs,
        gate: HostGate,
        inbox: CallInbox,
        run: Callable[[RunCancellation], RunEnd],
        finish: Callable[[RunEnd], ToolOutcome],
    ) -> None:
        self._inputs = inputs
        self._gate = gate
        self._inbox = inbox
        self._finish = finish
        self._outcome: ToolOutcome | None = None
        self._frames_taken = False

        # поля читателей выставлены до конструктора низа: он стартует поток
        super().__init__(tool, inputs.entries(), run)

    def inputs(self) -> Sequence[CallInputPort]:
        return self._inputs.slots()

    def done_sending(self) -> None:
        self._inputs.finish_all()

    def gate(self) -> CallGate:
        return self._gate

    def frames(self) -> Iterator[ToolFrame]:
        if self._frames_taken:
            msg = (
                f"{self._tool}: call frames already have a reader, "
                "frames() may be taken once per call"
            )
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
        self._gate.close()
