"""Запуск инструмента обычным субпроцессом хоста: dev-режим без песочницы.

Реализация порта ToolLauncher для разработки и отладки: команда модуля
исполняется интерпретатором приложения из workdir. Контракт процесса тот
же, что в песочнице (argv, кадры на stdin, каналы конверта/кадров/конфига
номерами в флагах --fd-*), но изоляции, cgroup-лимитов и прогрева модулей
нет. Механика исполнения общая с песочницей и живёт в boba.toolkit.pump:
вход тела пишет вызывающий через CallInput, каналы читает насос своим
потоком.

Ошибки:
ProcessCallError — процесс не запустился, не отдал конверт либо команда
    не является командой модуля инструментов.
ChannelOverflowError — канал вызова превысил потолок, вызов убит.
ToolStopped — вызов остановлен пользователем.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation
from boba.identity.context import CallContext
from boba.toolkit.chain import TappedCall
from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import EntryFlag
from boba.toolkit.frames import CallInbox
from boba.toolkit.launcher import (
    CappedChannel,
    ChannelTail,
    EnvelopeReply,
    LauncherError,
    LaunchOutcome,
    RunResult,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.protocol import ToolCommand
from boba.toolkit.pump import (
    CallInput,
    CallSinks,
    ChannelPump,
    OpenRun,
    PipePlumbing,
    PumpedCall,
)
from boba.toolkit.stream import ChunkSink

__all__ = [
    "ProcessCallError",
    "ProcessLauncherConfig",
    "ProcessToolCaller",
]

logger = logging.getLogger(__name__)


class ProcessCallError(LauncherError):
    """Субпроцесс нарушил контракт запуска: результату доверять нельзя."""


class ProcessLauncherConfig(BaseModel):
    """Секция конфига [tool_launcher] provider = process: параметры запуска
    без песочницы (workdir, шелл, таймаут и потолки каналов)."""

    model_config = ConfigDict(extra="ignore")

    provider: Literal["process"]

    workdir: str = Field(
        min_length=1,
        description="Рабочий каталог tool-процессов; файлы инструментов пишутся сюда.",
    )

    shell: str = Field(
        min_length=1,
        description="Шелл для текстовых команд (bash-инструмент).",
    )

    timeout_sec: float = Field(gt=0, description="Потолок времени одного вызова.")

    channel_limit_bytes: int = Field(
        gt=0,
        description="Потолок байтов канала вызова в памяти приложения.",
    )

    stderr_tail_bytes: int = Field(
        gt=0,
        description="Хвост stderr для объяснения сбоя, когда конверта нет.",
    )

    kill_grace_sec: float = Field(
        ge=0,
        description="Пауза между SIGTERM и SIGKILL при таймауте и отмене.",
    )


class _CallPipes:
    """Три пайпа вызова модуля: конверт (result) и кадры (frames) из тела,
    injected-конфиг в тело.

    Субпроцесс даёт из коробки только stdin/stdout/stderr — остальные
    каналы открываются здесь. Дескрипторы тела наследуются с теми же
    номерами (pass_fds), и эти номера дописываются в команду флагами
    --fd-result/--fd-frames/--injected-fd (argv_flags). У shell-команды
    пайпов модуля нет вовсе — все методы пусты.
    """

    def __init__(self, *, module: bool) -> None:
        self._module = module
        self._child_open = module
        self._injected_taken = False
        self._frames_taken = False

        self.result_r = -1
        self.result_w = -1
        self.frames_r = -1
        self.frames_w = -1
        self.injected_r = -1
        self.injected_w = -1

        if not module:
            return

        self.result_r, self.result_w = os.pipe()
        self.frames_r, self.frames_w = os.pipe()
        self.injected_r, self.injected_w = os.pipe()
        PipePlumbing.widen(self.frames_w)

    def argv_flags(self) -> tuple[str, ...]:
        """Флаги каналов для команды тела: номера унаследованных дескрипторов."""
        if not self._module:
            return ()

        return (
            EntryFlag.FD_RESULT.value,
            str(self.result_w),
            EntryFlag.FD_FRAMES.value,
            str(self.frames_w),
            EntryFlag.INJECTED_FD.value,
            str(self.injected_r),
        )

    def child_fds(self) -> tuple[int, ...]:
        if not self._module:
            return ()

        return (self.result_w, self.frames_w, self.injected_r)

    def take_injected(self) -> int:
        """Отдать канал конфига писателю: закрытия каналов его не трогают."""
        if not self._module:
            msg = (
                "process call pipes: the injected channel was asked for a "
                "non-module call that has no such channel"
            )
            raise LauncherError(msg)

        if self._injected_taken:
            msg = (
                "process call pipes: the injected channel was already taken "
                "by a writer, a second take is refused"
            )
            raise LauncherError(msg)

        self._injected_taken = True
        return self.injected_w

    def take_frames(self) -> int:
        """Отдать канал кадров перекачке: насос его не читает, закрытия
        каналов его не трогают; владеет дескриптором перекачка."""
        if not self._module:
            msg = (
                "process call pipes: the frames channel was asked for a "
                "non-module call that has no such channel"
            )
            raise LauncherError(msg)

        if self._frames_taken:
            msg = (
                "process call pipes: the frames channel was already taken "
                "by a reader, a second take is refused"
            )
            raise LauncherError(msg)

        self._frames_taken = True
        return self.frames_r

    def host_reads(self) -> tuple[tuple[ToolChannel, int], ...]:
        if not self._module:
            return ()

        reads: list[tuple[ToolChannel, int]] = [(ToolChannel.RESULT, self.result_r)]

        if not self._frames_taken:
            reads.append((ToolChannel.FRAMES, self.frames_r))

        return tuple(reads)

    def close_child_ends(self) -> None:
        if not self._child_open:
            return

        self._child_open = False
        for fd in (self.result_w, self.frames_w, self.injected_r):
            with suppress(OSError):
                os.close(fd)

    def close_host_ends(self) -> None:
        if not self._module:
            return

        self._module = False
        with suppress(OSError):
            os.close(self.result_r)

        if not self._frames_taken:
            with suppress(OSError):
                os.close(self.frames_r)

        if self._injected_taken:
            return

        with suppress(OSError):
            os.close(self.injected_w)

    def close_all(self) -> None:
        self.close_child_ends()
        self.close_host_ends()


@dataclass(frozen=True)
class _ProcRun:
    """Сырые процессные поля завершённого запуска; в RunResult их
    превращает _collect, добавив вывод каналов."""

    exit_code: int
    timed_out: bool
    duration_ms: int
    spawn_ms: int
    first_output_ms: int | None


@dataclass(frozen=True)
class _LiveCall:
    """Только что запущенное тело: процесс, его пайпы и дескриптор входа,
    который дальше заберёт CallInput. Возвращается из _spawn и живёт до
    конца прогона."""

    proc: subprocess.Popen[bytes]
    channels: _CallPipes
    stdin_w: int
    started: float
    spawn_ms: int


class _ProcessPump(ChannelPump):
    """Реализация ChannelPump для субпроцесса: завершение исполнителя
    определяется proc.poll(), добивание — сигналом группе процессов."""

    def __init__(
        self,
        poll_sec: float,
        timeout_sec: float,
        proc: subprocess.Popen[bytes],
        killer: Callable[[subprocess.Popen[bytes]], None],
    ) -> None:
        super().__init__(poll_sec, timeout_sec)
        self._proc = proc
        self._killer = killer

    def _finished(self) -> bool:
        return self._proc.poll() is not None

    def _kill(self) -> None:
        self._killer(self._proc)


class ProcessToolCaller(ToolLauncher):
    """Реализация протокола ToolLauncher субпроцессом хоста.

    open() спавнит тело и отдаёт PumpedCall для потокового вызова;
    call_text() исполняет shell-команду через тот же OpenRun. Создаётся
    фабрикой лончеров по одному на инструмент (имя идёт в логи).
    """

    ARGV_HEAD: ClassVar[int] = 3
    """python3 -m <module> — префикс команды модуля инструментов."""

    POLL_SEC: ClassVar[float] = 0.05

    MODULE_JOURNAL: ClassVar[tuple[ToolChannel, ...]] = (
        ToolChannel.STDOUT,
        ToolChannel.STDERR,
        ToolChannel.RESULT,
        ToolChannel.FRAMES,
    )
    """Каналы вызова модуля, попадающие в журнал при поставленном тапе."""

    SHELL_JOURNAL: ClassVar[tuple[ToolChannel, ...]] = (
        ToolChannel.STDOUT,
        ToolChannel.STDERR,
    )
    """Каналы shell-команды: конверта и кадров у неё нет."""

    def __init__(self, tool: str, cfg: ProcessLauncherConfig) -> None:
        self._tool = tool
        self._cfg = cfg

    def open(self, command: ToolCommand) -> ToolCall:
        """Вызов модуля инструментов: конфиг первым кадром, кадры тела наружу."""
        call, _fd = self._open_call(command, tap=False)

        return call

    def open_tap(self, command: ToolCommand) -> TappedCall:
        """Вызов-источник splice-перекачки (CallRelay.splice).

        Канал кадров хостом не разбирается и не журналируется — его
        дескриптор отдаётся перекачке; frames() такого вызова пуст.
        """
        call, fd = self._open_call(command, tap=True)

        return TappedCall(call=call, frames_fd=fd)

    def _open_call(self, command: ToolCommand, *, tap: bool) -> tuple[ToolCall, int]:
        """Общий открыватель вызова модуля; tap отдаёт канал кадров наружу."""
        argv = self._module_argv(command)

        envelope = CappedChannel(
            self._cfg.channel_limit_bytes, ToolChannel.RESULT.value
        )
        stderr_tail = ChannelTail(self._cfg.stderr_tail_bytes)
        inbox = CallInbox()

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.RESULT: envelope.feed,
            ToolChannel.STDERR: stderr_tail.feed,
        }
        journal = list(self.MODULE_JOURNAL)

        # сырой канал кадров хост не разбирает и не журналирует: без tap
        # насос дочитывает его в никуда, с tap — отдаёт перекачке
        if not tap and not command.raw_frames:
            own[ToolChannel.FRAMES] = inbox.feed
            journal.append(ToolChannel.FRAMES)

        sinks = CallSinks.merged(own, tuple(journal))

        live = self._spawn(argv, with_result=True)

        frames_fd = -1
        if tap:
            frames_fd = live.channels.take_frames()

        entry = CallSinks.stdin_input(live.stdin_w, framed=not command.raw_stdin)

        def run(cancellation: RunCancellation) -> _ProcRun:
            return self._pump_live(live, sinks, cancellation)

        def finish(run_end: _ProcRun) -> ToolOutcome:
            return self._collect(run_end, envelope, stderr_tail)

        try:
            call = PumpedCall(self._tool, entry, inbox, run, finish)
        except BaseException:
            # ход уже отменён: насос не родился, прибираем процесс сами
            entry.abandon()
            if frames_fd >= 0:
                with suppress(OSError):
                    os.close(frames_fd)
            self._kill(live.proc)
            live.proc.wait()
            live.channels.close_host_ends()
            self._close_pipes(live.proc)
            raise

        # насос уже жив: запись конфига блокируется только скоростью тела
        config_input = CallInput(live.channels.take_injected())
        config_input.send_bytes(command.config)
        config_input.finish()

        return call, frames_fd

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Shell-команда на хосте: stdout/stderr/rc как есть."""
        limit = self._cfg.channel_limit_bytes
        stdout = CappedChannel(limit, ToolChannel.STDOUT.value)
        stderr = CappedChannel(limit, ToolChannel.STDERR.value)

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.STDOUT: stdout.feed,
            ToolChannel.STDERR: stderr.feed,
        }
        sinks = CallSinks.merged(own, self.SHELL_JOURNAL)

        argv = (self._cfg.shell, "-c", command)
        live = self._spawn(argv, with_result=False)
        entry = CallInput(live.stdin_w)

        def pump_run(cancellation: RunCancellation) -> _ProcRun:
            return self._pump_live(live, sinks, cancellation)

        try:
            opened = OpenRun(self._tool, entry, pump_run)
        except BaseException:
            # ход уже отменён: насос не родился, прибираем процесс сами
            entry.abandon()
            self._kill(live.proc)
            live.proc.wait()
            live.channels.close_host_ends()
            self._close_pipes(live.proc)
            raise

        entry.send_bytes(stdin.encode("utf-8"))
        entry.finish()

        outcome = opened.wait()

        run = RunResult(
            exit_code=outcome.exit_code,
            stdout=stdout.text(),
            stderr=stderr.text(),
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
            spawn_ms=outcome.spawn_ms,
            first_output_ms=outcome.first_output_ms,
        )

        if run.exit_code != 0:
            self.log_failure(run)

        return LaunchOutcome(self._tool, run, "")

    def log_failure(self, run: RunResult) -> None:
        logger.warning(
            "process[%s]: rc=%d timed_out=%s stderr=%r",
            self._tool,
            run.exit_code,
            run.timed_out,
            run.stderr,
        )

    def _module_argv(self, command: ToolCommand) -> tuple[str, ...]:
        """Команда модуля интерпретатором приложения вместо python3 из PATH."""
        argv = command.argv
        if len(argv) <= self.ARGV_HEAD:
            msg = (
                f"{self._tool}: not a tool module command, expected "
                f"python -m <module> <tool> ..., got {argv[:3]}"
            )
            raise ProcessCallError(msg)

        if argv[1] != "-m":
            msg = (
                f"{self._tool}: not a tool module command, expected "
                f"python -m <module> <tool> ..., got {argv[:3]}"
            )
            raise ProcessCallError(msg)

        # интерпретатор приложения вместо python3 из PATH образа песочницы
        return (sys.executable, *argv[1:])

    def _call_workdir(self) -> str:
        """Рабочий каталог тела: своя папка области вызова, как /workspace в песочнице.

        Вне контекста вызова (прогрев, пробы) тело работает в общем workdir.
        """
        context = CallContext.peek()
        if context is None:
            return self._cfg.workdir

        scoped = Path(self._cfg.workdir) / context.scope.id
        scoped.mkdir(parents=True, exist_ok=True)
        return str(scoped)

    def _spawn(self, argv: Sequence[str], *, with_result: bool) -> _LiveCall:
        """Запустить тело с каналами; спавн идёт в потоке вызывающего.

        Здесь же снимаются контексты вызова (workdir области, журнальный тап):
        в поток насоса contextvar'ы не переезжают.
        """
        workdir = self._call_workdir()

        channels = _CallPipes(module=with_result)
        stdin_r, stdin_w = os.pipe()
        PipePlumbing.widen(stdin_w)

        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv собран контрактом модуля
                [*argv, *channels.argv_flags()],
                stdin=stdin_r,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=dict(os.environ),
                pass_fds=channels.child_fds(),
                start_new_session=True,
            )
        except OSError as exc:
            channels.close_all()
            os.close(stdin_r)
            os.close(stdin_w)

            msg = f"{self._tool}: spawn of {argv[0]} in {workdir} failed: {exc}"
            raise ProcessCallError(msg) from exc

        spawn_ms = int((time.monotonic() - started) * 1000)

        # копии записи родителя закрываются сразу: EOF каналов наступает
        # вместе со смертью тела
        os.close(stdin_r)
        channels.close_child_ends()

        return _LiveCall(
            proc=proc,
            channels=channels,
            stdin_w=stdin_w,
            started=started,
            spawn_ms=spawn_ms,
        )

    def _pump_live(
        self,
        live: _LiveCall,
        sinks: Mapping[ToolChannel, ChunkSink],
        cancellation: RunCancellation,
    ) -> _ProcRun:
        """Прогнать каналы тела до его выхода; зовут call_text и поток насоса."""
        pump = _ProcessPump(self.POLL_SEC, self._cfg.timeout_sec, live.proc, self._kill)
        self._register_reads(pump, live, sinks)

        try:
            end = pump.run(cancellation)
        except BaseException:
            # сорвался приёмник или пришла отмена: тело добивается группой,
            # иначе оно переживёт вызов и продолжит писать в закрытые пайпы
            self._kill(live.proc)
            live.proc.wait()
            raise
        finally:
            pump.close()
            live.channels.close_host_ends()
            self._close_pipes(live.proc)

        return _ProcRun(
            exit_code=live.proc.wait(),
            timed_out=end.timed_out,
            duration_ms=int((time.monotonic() - live.started) * 1000),
            spawn_ms=live.spawn_ms,
            first_output_ms=end.first_output_ms,
        )

    @staticmethod
    def _register_reads(
        pump: ChannelPump,
        live: _LiveCall,
        sinks: Mapping[ToolChannel, ChunkSink],
    ) -> None:
        """Каналы тела в насос; канал без приёмника дочитывается в никуда."""
        reads: list[tuple[ToolChannel, int]] = []

        stdout = live.proc.stdout
        if stdout is not None:
            reads.append((ToolChannel.STDOUT, stdout.fileno()))

        stderr = live.proc.stderr
        if stderr is not None:
            reads.append((ToolChannel.STDERR, stderr.fileno()))

        reads.extend(live.channels.host_reads())

        for channel, fd in reads:
            sink = sinks.get(channel)
            if sink is None:
                pump.add_drain(fd)
                continue

            pump.add_read(fd, sink)

    def _collect(
        self,
        run: _ProcRun,
        envelope: CappedChannel,
        stderr_tail: ChannelTail,
    ) -> ToolOutcome:
        """Итог вызова модуля: процессные поля плюс разбор конверта."""
        result = RunResult(
            exit_code=run.exit_code,
            stdout="",
            stderr=stderr_tail.text(),
            duration_ms=run.duration_ms,
            timed_out=run.timed_out,
            spawn_ms=run.spawn_ms,
            first_output_ms=run.first_output_ms,
        )

        if result.exit_code != 0:
            self.log_failure(result)

        reply = EnvelopeReply.parse(self._tool, envelope.data(), result, "")

        return ToolOutcome(reply=reply, run=result, diagnostic="")

    def _kill(self, proc: subprocess.Popen[bytes]) -> None:
        """Гасит группу тела: SIGTERM, пауза, SIGKILL выжившим."""
        if proc.poll() is not None:
            return

        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)

        try:
            proc.wait(timeout=self._cfg.kill_grace_sec)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    @staticmethod
    def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue

            with suppress(OSError):
                stream.close()
