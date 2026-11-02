"""Запуск инструмента обычным субпроцессом хоста: dev-режим без песочницы.

Реализация порта ToolLauncher для разработки и отладки: подкоманда call
программы плагина исполняется интерпретатором приложения из workdir.
Контракт процесса тот же, что в песочнице (argv, кадры на stdin, каналы
конверта/кадров/аргументов номерами в флагах --fd-*), но изоляции,
cgroup-лимитов и прогрева модулей нет. Механика исполнения общая с
песочницей и живёт в boba.tool_io.pump: вход тела пишет вызывающий через
CallInput, каналы читает насос своим потоком.

Ошибки:
ProcessCallError — процесс не запустился либо не отдал конверт.
ChannelOverflowError — канал вызова превысил потолок, вызов убит.
ToolStopped — вызов остановлен пользователем.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation
from boba.identity.context import CallContexts
from boba.tool_io.chain import CallAmbient
from boba.tool_io.channels import PipeSetup, StreamPipes
from boba.tool_io.entry import EntryFlag, InputWire, OutputWire
from boba.tool_io.frames import CallInbox
from boba.tool_io.pump import (
    CallInput,
    CallSinks,
    ChannelPump,
    HostGate,
    PumpedCall,
)
from boba.toolkit.channels import ToolChannel
from boba.toolkit.launcher import (
    CappedChannel,
    ChannelTail,
    EnvelopeReply,
    LauncherError,
    RunResult,
    TappedCall,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.protocol import CallInputSpec, CallOutputSpec, ToolCommand
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
    без песочницы (таймаут и потолки каналов). Рабочий каталог — workspace
    данных экземпляра, его называет способ запуска, а не конфиг."""

    model_config = ConfigDict(extra="ignore")

    provider: Literal["process"]

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
    """Пайпы вызова модуля сверх stdio: конверт (result), отчёты о ходе
    работы (progress), выходы и готовность барьера (gate) из тела,
    аргументы вызова, ответ барьера (verdict) и входы после первого — в тело.

    Субпроцесс даёт из коробки только stdin/stdout/stderr — остальные
    каналы открываются здесь. Входы и выходы симметричны: у каждого канала
    свой пайп; stdin процесса порта не несёт. Дескрипторы тела наследуются
    с теми же номерами (pass_fds), и эти номера дописываются в команду
    флагами --fd-result/--args-fd/--fd-gate/--fd-verdict/--fd-progress/
    --fd-in/--fd-out (argv_flags). Портами владеет StreamPipes: входы забирает
    CallInputs, выходы — перекачка либо насос. Концы барьера передаются HostGate.
    """

    def __init__(
        self, inputs: Sequence[CallInputSpec], outputs: Sequence[CallOutputSpec]
    ) -> None:
        self._host_open = True
        self._child_open = True
        self._args_taken = False
        self._gate_taken = False
        self._specs = tuple(inputs)
        self._out_specs = tuple(outputs)

        with PipeSetup() as setup:
            self.result_r, self.result_w = setup.pipe()
            self.args_r, self.args_w = setup.pipe()
            self.gate_r, self.gate_w = setup.pipe()
            self.verdict_r, self.verdict_w = setup.pipe()
            self.progress_r, self.progress_w = setup.pipe()
            self.streams = StreamPipes(setup, inputs, outputs, self._close_fd)
            setup.pop_all()

    def argv_flags(self) -> tuple[str, ...]:
        """Флаги каналов для команды тела: номера унаследованных дескрипторов."""
        flags = [
            EntryFlag.FD_RESULT.value,
            str(self.result_w),
            EntryFlag.ARGS_FD.value,
            str(self.args_r),
            EntryFlag.FD_GATE.value,
            str(self.gate_w),
            EntryFlag.FD_VERDICT.value,
            str(self.verdict_r),
            EntryFlag.FD_PROGRESS.value,
            str(self.progress_w),
        ]
        for spec, read_fd in zip(self._specs, self.streams.child_inputs(), strict=True):
            flags.extend(InputWire(port=spec.port, fd=read_fd).argv())

        for spec, write_fd in zip(
            self._out_specs, self.streams.child_outputs(), strict=True
        ):
            flags.extend(OutputWire(port=spec.port, fd=write_fd).argv())

        return tuple(flags)

    def child_fds(self) -> tuple[int, ...]:
        fds = [
            self.result_w,
            self.args_r,
            self.gate_w,
            self.verdict_r,
            self.progress_w,
        ]
        fds.extend(self.streams.child_inputs())
        fds.extend(self.streams.child_outputs())

        return tuple(fds)

    def take_verdict(self) -> int:
        """Отдать запись ответа барьера HostGate; пайп готовности gate_r
        остаётся здесь — его читает насос, закрывает close_host_ends."""
        if self._gate_taken:
            msg = (
                "process call pipes: the verdict channel was already taken "
                "by the call gate, a second take is refused"
            )
            raise LauncherError(msg)

        self._gate_taken = True
        return self.verdict_w

    def take_args(self) -> int:
        """Отдать канал конфига писателю: закрытия каналов его не трогают."""
        if self._args_taken:
            msg = (
                "process call pipes: the arguments channel was already taken "
                "by a writer, a second take is refused"
            )
            raise LauncherError(msg)

        self._args_taken = True
        return self.args_w

    def host_reads(self) -> tuple[tuple[ToolChannel, int], ...]:
        reads: list[tuple[ToolChannel, int]] = [
            (ToolChannel.RESULT, self.result_r),
            (ToolChannel.PROGRESS, self.progress_r),
        ]

        reads.extend(self.streams.frame_reads())

        return tuple(reads)

    def close_child_ends(self) -> None:
        if not self._child_open:
            return

        self._child_open = False
        for fd in self.child_fds():
            self._close_fd(fd)

    def close_host_ends(self) -> None:
        if not self._host_open:
            return

        self._host_open = False
        self._close_fd(self.result_r)
        self._close_fd(self.progress_r)
        self.streams.close_host_ends()
        self._close_fd(self.gate_r)

        if not self._gate_taken:
            self._close_fd(self.verdict_w)

        if not self._args_taken:
            self._close_fd(self.args_w)

    def _close_fd(self, fd: int) -> None:
        with suppress(OSError):
            os.close(fd)

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

    open() спавнит тело и отдаёт PumpedCall для потокового вызова.
    Создаётся способом запуска процессом хоста (ProcessLaunchers) на плагин
    и стендами тестов. program — префикс команды вызова до имени
    инструмента: у плагина `python -m boba.sandbox.program <id> call`;
    workdir — рабочий каталог тел, свой подкаталог на область вызова.
    """

    POLL_SEC: ClassVar[float] = 0.05

    MODULE_JOURNAL: ClassVar[tuple[ToolChannel, ...]] = (
        ToolChannel.STDOUT,
        ToolChannel.STDERR,
        ToolChannel.RESULT,
        ToolChannel.FRAMES,
        ToolChannel.PROGRESS,
    )
    """Каналы вызова модуля, попадающие в журнал при поставленном тапе."""

    def __init__(  # noqa: PLR0913 — вызов собирается конфигом, каталогом и контекстом
        self,
        tool: str,
        cfg: ProcessLauncherConfig,
        workdir: str,
        contexts: CallContexts,
        ambient: CallAmbient,
        program: Sequence[str],
    ) -> None:
        self._tool = tool
        self._program = tuple(program)
        self._cfg = cfg
        self._workdir = workdir
        self._contexts = contexts
        self._ambient = ambient
        self._sinks = CallSinks()

    def open(self, command: ToolCommand) -> ToolCall:
        """Вызов модуля инструментов: конфиг первым кадром, кадры тела наружу."""
        call, _fd = self._open_call(command, tap=False)

        return call

    def open_tap(self, command: ToolCommand) -> TappedCall:
        """Вызов-источник splice-перекачки (раздача каналов группы).

        Выходные каналы хостом не разбираются и не журналируются — их
        дескрипторы отдаются перекачке; frames() такого вызова пуст.
        """
        call, fds = self._open_call(command, tap=True)

        return TappedCall(call=call, frames_fds=fds)

    def _open_call(
        self, command: ToolCommand, *, tap: bool
    ) -> tuple[ToolCall, tuple[int, ...]]:
        """Общий открыватель вызова модуля; tap отдаёт выходы наружу."""
        argv = (*self._program, *command.argv)

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

        live = self._spawn(argv, command.inputs, command.outputs)

        # хост читает кадры лишь у единственного кадрового выхода без tap;
        # прочие выходы без читателя насос дочитывает в никуда
        frames_fds: tuple[int, ...] = ()
        if tap:
            frames_fds = live.channels.streams.take_outputs()
        elif live.channels.streams.claim_frames() is not None:
            own[ToolChannel.FRAMES] = inbox.feed
            journal.append(ToolChannel.FRAMES)

        call_journal = self._ambient.sinks()
        sinks = self._sinks.merged(own, tuple(journal), call_journal)

        inputs = self._sinks.call_inputs(
            live.stdin_w,
            live.channels.streams.take_inputs(),
            command.inputs,
            call_journal,
        )
        gate = HostGate(live.channels.take_verdict(), command.gate)

        def run(cancellation: RunCancellation) -> _ProcRun:
            return self._pump_live(live, sinks, gate, cancellation)

        def finish(run_end: _ProcRun) -> ToolOutcome:
            return self._collect(run_end, envelope, stderr_tail)

        try:
            call = PumpedCall(self._tool, inputs, gate, inbox, run, finish)
        except BaseException:
            # ход уже отменён: насос не родился, прибираем процесс сами
            inputs.abandon_all()
            gate.close()
            for fd in frames_fds:
                with suppress(OSError):
                    os.close(fd)
            self._kill(live.proc)
            live.proc.wait()
            live.channels.close_host_ends()
            self._close_pipes(live.proc)
            raise

        # насос уже жив: запись конфига блокируется только скоростью тела
        args_input = CallInput(live.channels.take_args())
        args_input.send_bytes(command.args)
        args_input.finish()

        return call, frames_fds

    def log_failure(self, run: RunResult) -> None:
        logger.warning(
            "process[%s]: rc=%d timed_out=%s stderr=%r",
            self._tool,
            run.exit_code,
            run.timed_out,
            run.stderr,
        )

    def _call_workdir(self) -> str:
        """Рабочий каталог тела: своя папка области вызова, как /workspace в песочнице.

        Вне контекста вызова (прогрев, пробы) тело работает в общем workdir.
        """
        context = self._contexts.peek()
        if context is None:
            return self._workdir

        scoped = Path(self._workdir) / context.scope.id
        scoped.mkdir(parents=True, exist_ok=True)
        return str(scoped)

    def _spawn(
        self,
        argv: Sequence[str],
        inputs: Sequence[CallInputSpec],
        outputs: Sequence[CallOutputSpec],
    ) -> _LiveCall:
        """Запустить тело с каналами; спавн идёт в потоке вызывающего.

        Здесь же снимаются контексты вызова (workdir области, журнальный тап):
        в поток насоса contextvar'ы не переезжают.
        """
        workdir = self._call_workdir()

        try:
            with PipeSetup() as setup:
                channels = _CallPipes(inputs, outputs)
                setup.callback(channels.close_all)
                stdin_r, stdin_w = setup.pipe()
                started = time.monotonic()
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
                setup.pop_all()
        except OSError as exc:
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
        gate: HostGate,
        cancellation: RunCancellation,
    ) -> _ProcRun:
        """Прогнать каналы тела до его выхода; зовёт поток насоса вызова."""
        pump = _ProcessPump(self.POLL_SEC, self._cfg.timeout_sec, live.proc, self._kill)
        self._register_reads(pump, live, sinks)
        gate.watch(pump, live.channels.gate_r)

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

        for fd in live.channels.streams.drain_reads():
            pump.add_drain(fd)

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
