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
from boba.toolkit.entry import EntryFlag, InputWire, OutputWire
from boba.toolkit.frames import CallInbox
from boba.toolkit.launcher import (
    CappedChannel,
    ChannelTail,
    EnvelopeReply,
    LauncherError,
    RunResult,
    ToolCall,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.protocol import CallInputSpec, CallOutputSpec, ToolCommand
from boba.toolkit.pump import (
    CallInput,
    CallSinks,
    ChannelPump,
    HostGate,
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
    """Пайпы вызова модуля сверх stdio: конверт (result), выходы и
    готовность барьера (gate) из тела, injected-конфиг, ответ барьера
    (verdict) и входы после первого — в тело.

    Субпроцесс даёт из коробки только stdin/stdout/stderr — остальные
    каналы открываются здесь. Входы и выходы симметричны: у каждого канала
    свой пайп; stdin процесса порта не несёт. Дескрипторы тела наследуются
    с теми же номерами (pass_fds), и эти номера дописываются в команду
    флагами --fd-result/--injected-fd/--fd-gate/--fd-verdict/--fd-in/--fd-out
    (argv_flags). Записывающие концы входов забирает CallInputs вызова
    (take_inputs), концы барьера — HostGate (take_verdict), читающие концы
    выходов — перекачка (take_outputs) либо насос (claim_frames).
    """

    def __init__(
        self, inputs: Sequence[CallInputSpec], outputs: Sequence[CallOutputSpec]
    ) -> None:
        self._host_open = True
        self._child_open = True
        self._injected_taken = False
        self._outputs_taken = False
        self._claimed: int | None = None
        self._inputs_taken = False
        self._gate_taken = False
        self._specs = tuple(inputs)
        self._out_specs = tuple(outputs)

        self.result_r, self.result_w = os.pipe()
        self.injected_r, self.injected_w = os.pipe()
        self.gate_r, self.gate_w = os.pipe()
        self.verdict_r, self.verdict_w = os.pipe()

        self._ins: list[tuple[int, int]] = []
        for spec in self._specs:
            read_fd, write_fd = os.pipe()
            PipePlumbing.require(write_fd, spec.pipe_bytes, spec.port)
            self._ins.append((read_fd, write_fd))

        self._outs: list[tuple[int, int]] = []
        for out_spec in self._out_specs:
            read_fd, write_fd = os.pipe()
            PipePlumbing.require(write_fd, out_spec.pipe_bytes, out_spec.port)
            self._outs.append((read_fd, write_fd))

    def argv_flags(self) -> tuple[str, ...]:
        """Флаги каналов для команды тела: номера унаследованных дескрипторов."""
        flags = [
            EntryFlag.FD_RESULT.value,
            str(self.result_w),
            EntryFlag.INJECTED_FD.value,
            str(self.injected_r),
            EntryFlag.FD_GATE.value,
            str(self.gate_w),
            EntryFlag.FD_VERDICT.value,
            str(self.verdict_r),
        ]
        for spec, (read_fd, _) in zip(self._specs, self._ins, strict=True):
            flags.extend(InputWire(port=spec.port, fd=read_fd).argv())

        for spec, (_, write_fd) in zip(self._out_specs, self._outs, strict=True):
            flags.extend(OutputWire(port=spec.port, fd=write_fd).argv())

        return tuple(flags)

    def child_fds(self) -> tuple[int, ...]:
        fds = [
            self.result_w,
            self.injected_r,
            self.gate_w,
            self.verdict_r,
        ]
        for read_fd, _ in self._ins:
            fds.append(read_fd)

        for _, write_fd in self._outs:
            fds.append(write_fd)

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

    def take_inputs(self) -> tuple[int, ...]:
        """Отдать записывающие концы входов их владельцу — CallInputs
        вызова; закрытия каналов их больше не трогают."""
        if self._inputs_taken:
            msg = (
                "process call pipes: the input channels were already taken "
                "by the call inputs, a second take is refused"
            )
            raise LauncherError(msg)

        self._inputs_taken = True

        fds: list[int] = []
        for _, write_fd in self._ins:
            fds.append(write_fd)

        return tuple(fds)

    def take_injected(self) -> int:
        """Отдать канал конфига писателю: закрытия каналов его не трогают."""
        if self._injected_taken:
            msg = (
                "process call pipes: the injected channel was already taken "
                "by a writer, a second take is refused"
            )
            raise LauncherError(msg)

        self._injected_taken = True
        return self.injected_w

    def take_outputs(self) -> tuple[int, ...]:
        """Отдать читающие концы всех выходов перекачке: насос их не читает,
        закрытия каналов их не трогают; владеет дескрипторами перекачка."""
        if self._outputs_taken or self._claimed is not None:
            msg = (
                "process call pipes: the output channels were already taken "
                "by a reader, a second take is refused"
            )
            raise LauncherError(msg)

        self._outputs_taken = True

        fds: list[int] = []
        for read_fd, _ in self._outs:
            fds.append(read_fd)

        return tuple(fds)

    def claim_frames(self) -> int | None:
        """Единственный кадровый выход — насосу: его кадры читает хост.

        None — выходов нет, их больше одного либо выход сырой: хосту в них
        смотреть незачем, непрошенные дочитает drain_reads.
        """
        if self._outputs_taken:
            msg = (
                "process call pipes: the output channels are taken by a "
                "reader, claiming frames for the host is refused"
            )
            raise LauncherError(msg)

        if len(self._out_specs) != 1:
            return None

        if self._out_specs[0].raw:
            return None

        self._claimed = 0
        return self._outs[0][0]

    def drain_reads(self) -> tuple[int, ...]:
        """Читающие концы выходов без читателя: насос дочитывает их в никуда,
        чтобы тело не встало на записи."""
        if self._outputs_taken:
            return ()

        fds: list[int] = []
        for index, (read_fd, _) in enumerate(self._outs):
            if index == self._claimed:
                continue

            fds.append(read_fd)

        return tuple(fds)

    def host_reads(self) -> tuple[tuple[ToolChannel, int], ...]:
        reads: list[tuple[ToolChannel, int]] = [(ToolChannel.RESULT, self.result_r)]

        if self._claimed is not None:
            reads.append((ToolChannel.FRAMES, self._outs[self._claimed][0]))

        return tuple(reads)

    def close_child_ends(self) -> None:
        if not self._child_open:
            return

        self._child_open = False
        for fd in self.child_fds():
            with suppress(OSError):
                os.close(fd)

    def close_host_ends(self) -> None:
        if not self._host_open:
            return

        self._host_open = False
        with suppress(OSError):
            os.close(self.result_r)

        if not self._outputs_taken:
            for read_fd, _ in self._outs:
                with suppress(OSError):
                    os.close(read_fd)

        if not self._inputs_taken:
            for _, write_fd in self._ins:
                with suppress(OSError):
                    os.close(write_fd)

        with suppress(OSError):
            os.close(self.gate_r)

        if not self._gate_taken:
            with suppress(OSError):
                os.close(self.verdict_w)

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

    open() спавнит тело и отдаёт PumpedCall для потокового вызова.
    Создаётся фабрикой лончеров по одному на инструмент (имя идёт в логи).
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

    def __init__(self, tool: str, cfg: ProcessLauncherConfig) -> None:
        self._tool = tool
        self._cfg = cfg

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

        live = self._spawn(argv, command.inputs, command.outputs)

        # хост читает кадры лишь у единственного кадрового выхода без tap;
        # прочие выходы без читателя насос дочитывает в никуда
        frames_fds: tuple[int, ...] = ()
        if tap:
            frames_fds = live.channels.take_outputs()
        elif live.channels.claim_frames() is not None:
            own[ToolChannel.FRAMES] = inbox.feed
            journal.append(ToolChannel.FRAMES)

        sinks = CallSinks.merged(own, tuple(journal))

        inputs = CallSinks.call_inputs(
            live.stdin_w, live.channels.take_inputs(), command.inputs
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
        config_input = CallInput(live.channels.take_injected())
        config_input.send_bytes(command.config)
        config_input.finish()

        return call, frames_fds

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

        channels = _CallPipes(inputs, outputs)
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

        for fd in live.channels.drain_reads():
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
