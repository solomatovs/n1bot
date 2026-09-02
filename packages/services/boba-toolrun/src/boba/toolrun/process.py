"""Запуск инструмента обычным субпроцессом хоста: dev-режим без песочницы.

Команда модуля исполняется интерпретатором приложения из workdir; контракт
процесса тот же, что в песочнице (argv, кадры на stdin и в BOBA_FD_FRAMES,
конверт через BOBA_FD_RESULT), но изоляции, cgroup-лимитов и прогрева
модулей нет. Вход тела пишется напрямую в пайп потоком вызывающего; насос
своим потоком только читает каналы.

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

from boba.cancellation import RunCancellation, current_cancellation
from boba.identity.context import CallContext
from boba.toolkit.channels import ToolChannel
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
    CallSinks,
    ChannelPump,
    FrameInput,
    InputFeeder,
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
    """Секция [tool_launcher] provider = process: запуск без песочницы."""

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
    """Пайпы каналов вызова, которых нет у subprocess: result и frames.

    Дескрипторы записи наследует тело (их номера едут в env), дескрипторы
    чтения держит насос.
    """

    def __init__(self, ends: Mapping[ToolChannel, tuple[int, int]]) -> None:
        self._ends = dict(ends)
        self._child_open = True

    @classmethod
    def opened(cls, env: dict[str, str], *, with_result: bool) -> _CallPipes:
        """Открывает пайпы вызова модуля и прописывает их номера в env."""
        if not with_result:
            return cls({})

        ends: dict[ToolChannel, tuple[int, int]] = {}
        for channel in (ToolChannel.RESULT, ToolChannel.FRAMES):
            read_fd, write_fd = os.pipe()
            os.set_inheritable(write_fd, True)
            env[channel.env_name] = str(write_fd)
            ends[channel] = (read_fd, write_fd)

        return cls(ends)

    def child_fds(self) -> tuple[int, ...]:
        fds: list[int] = []
        for _, write_fd in self._ends.values():
            fds.append(write_fd)

        return tuple(fds)

    def host_reads(self) -> tuple[tuple[ToolChannel, int], ...]:
        reads: list[tuple[ToolChannel, int]] = []
        for channel, (read_fd, _) in self._ends.items():
            reads.append((channel, read_fd))

        return tuple(reads)

    def close_child_ends(self) -> None:
        if not self._child_open:
            return

        self._child_open = False
        for _, write_fd in self._ends.values():
            with suppress(OSError):
                os.close(write_fd)

    def close_host_ends(self) -> None:
        for read_fd, _ in self._ends.values():
            with suppress(OSError):
                os.close(read_fd)

        self._ends.clear()

    def close_all(self) -> None:
        self.close_child_ends()
        self.close_host_ends()


@dataclass(frozen=True)
class _ProcRun:
    """Процессные поля одного запуска до сборки RunResult."""

    exit_code: int
    timed_out: bool
    duration_ms: int
    spawn_ms: int
    first_output_ms: int | None


@dataclass(frozen=True)
class _LiveCall:
    """Запущенное тело: процесс, пайпы каналов и вход, ещё не отданный писателю."""

    proc: subprocess.Popen[bytes]
    channels: _CallPipes
    stdin_w: int
    started: float
    spawn_ms: int


class _ProcessPump(ChannelPump):
    """Насос субпроцесса: жизнь исполнителя по poll, добивание группой."""

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
    """Реализация ToolLauncher субпроцессом хоста: команда модуля -> конверт."""

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
        argv = self._module_argv(command)

        envelope = CappedChannel(
            self._cfg.channel_limit_bytes, ToolChannel.RESULT.value
        )
        stderr_tail = ChannelTail(self._cfg.stderr_tail_bytes)
        inbox = CallInbox()

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.RESULT: envelope.feed,
            ToolChannel.STDERR: stderr_tail.feed,
            ToolChannel.FRAMES: inbox.feed,
        }
        sinks = CallSinks.merged(own, self.MODULE_JOURNAL)

        live = self._spawn(argv, with_result=True)
        entry = FrameInput(live.stdin_w)

        def run(cancellation: RunCancellation) -> _ProcRun:
            return self._pump_live(live, sinks, cancellation)

        def finish(run_end: _ProcRun) -> ToolOutcome:
            return self._collect(run_end, envelope, stderr_tail)

        try:
            call = PumpedCall(self._tool, entry, inbox, run, finish)
        except BaseException:
            # ход уже отменён: насос не родился, прибираем процесс сами
            entry.abandon()
            self._kill(live.proc)
            live.proc.wait()
            live.channels.close_host_ends()
            self._close_pipes(live.proc)
            raise

        entry.send_config(command.config)
        return call

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
        feeder = InputFeeder(live.stdin_w, stdin.encode("utf-8"))

        try:
            outcome = self._pump_live(live, sinks, current_cancellation())
        finally:
            feeder.join()

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
            msg = f"{self._tool}: not a tool module command: {argv[:3]}"
            raise ProcessCallError(msg)

        if argv[1] != "-m":
            msg = f"{self._tool}: not a tool module command: {argv[:3]}"
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
        env = dict(os.environ)

        channels = _CallPipes.opened(env, with_result=with_result)
        stdin_r, stdin_w = os.pipe()

        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv собран контрактом модуля
                list(argv),
                stdin=stdin_r,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=env,
                pass_fds=channels.child_fds(),
                start_new_session=True,
            )
        except OSError as exc:
            channels.close_all()
            os.close(stdin_r)
            os.close(stdin_w)

            msg = f"{self._tool}: cannot spawn {argv[0]}: {exc}"
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
        pump = _ProcessPump(
            self.POLL_SEC, self._cfg.timeout_sec, live.proc, self._kill
        )
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
