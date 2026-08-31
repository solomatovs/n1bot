"""Запуск инструмента обычным субпроцессом хоста: dev-режим без песочницы.

Команда модуля исполняется интерпретатором приложения из workdir; контракт
процесса тот же, что в песочнице (argv, injected на stdin, конверт через fd
BOBA_FD_TOOL_RESULT), но изоляции, cgroup-лимитов и прогрева модулей нет.

Ошибки:
ProcessCallError — процесс не запустился, не отдал конверт либо команда
    не является командой модуля инструментов.
ChannelOverflowError — канал вызова превысил потолок, вызов убит.
ToolStopped — вызов остановлен пользователем.
"""

from __future__ import annotations

import logging
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import current_cancellation
from boba.toolkit.channels import ToolChannel
from boba.toolkit.launcher import (
    CappedChannel,
    ChannelTail,
    LauncherError,
    LaunchOutcome,
    RunResult,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.protocol import REPLY, ToolCommand
from boba.toolkit.stream import Chunk, ChunkSink, ToolChannelsTap

__all__ = ["ProcessCallError", "ProcessLauncherConfig", "ProcessToolCaller"]

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


class _Tee:
    """Тройник двух приёмников одного канала."""

    def __init__(self, first: ChunkSink, second: ChunkSink) -> None:
        self._first = first
        self._second = second

    def feed(self, chunk: Chunk) -> None:
        self._first(chunk)
        self._second(chunk)


@dataclass(frozen=True)
class _PumpEnd:
    """Итог насоса каналов: код возврата, таймаут, латентность первого байта."""

    exit_code: int
    timed_out: bool
    first_output_ms: int | None


@dataclass(frozen=True)
class _ProcRun:
    """Процессные поля одного запуска до сборки RunResult."""

    exit_code: int
    timed_out: bool
    duration_ms: int
    spawn_ms: int
    first_output_ms: int | None


class ProcessToolCaller(ToolLauncher):
    """Реализация ToolLauncher субпроцессом хоста: команда модуля -> конверт."""

    ARGV_HEAD: ClassVar[int] = 3
    """python3 -m <module> — префикс команды модуля инструментов."""

    POLL_SEC: ClassVar[float] = 0.05
    READ_BYTES: ClassVar[int] = 65536

    def __init__(self, tool: str, cfg: ProcessLauncherConfig) -> None:
        self._tool = tool
        self._cfg = cfg

    def run_tool(self, command: ToolCommand) -> ToolOutcome:
        """Граница слоя: наружу только ошибки из контракта модуля."""
        argv = self._module_argv(command)

        envelope = CappedChannel(
            self._cfg.channel_limit_bytes, ToolChannel.RESULT.value
        )
        stderr_tail = ChannelTail(self._cfg.stderr_tail_bytes)

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.RESULT: envelope.feed,
            ToolChannel.STDERR: stderr_tail.feed,
        }
        outcome = self._run(argv, command.stdin, own, with_result=True)

        run = RunResult(
            exit_code=outcome.exit_code,
            stdout="",
            stderr=stderr_tail.text(),
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
            spawn_ms=outcome.spawn_ms,
            first_output_ms=outcome.first_output_ms,
        )

        if run.exit_code != 0:
            self._log_failure(run)

        reply_raw = envelope.data()
        if not reply_raw:
            msg = (
                f"{self._tool}: no envelope on tool_result "
                f"(rc={run.exit_code}, timed_out={run.timed_out}); "
                f"tool_stderr={run.stderr!r}"
            )
            raise ProcessCallError(msg)

        try:
            reply = REPLY.validate_json(reply_raw)
        except ValueError as exc:
            msg = f"{self._tool}: envelope does not match contract: {exc}"
            raise ProcessCallError(msg) from exc

        return ToolOutcome(reply=reply, run=run, diagnostic="")

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Shell-команда на хосте: stdout/stderr/rc как есть."""
        limit = self._cfg.channel_limit_bytes
        stdout = CappedChannel(limit, ToolChannel.STDOUT.value)
        stderr = CappedChannel(limit, ToolChannel.STDERR.value)

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.STDOUT: stdout.feed,
            ToolChannel.STDERR: stderr.feed,
        }
        argv = (self._cfg.shell, "-c", command)
        outcome = self._run(argv, stdin.encode("utf-8"), own, with_result=False)

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
            self._log_failure(run)

        return LaunchOutcome(self._tool, run, "")

    def _log_failure(self, run: RunResult) -> None:
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

    def _sinks(
        self, own: Mapping[ToolChannel, ChunkSink]
    ) -> dict[ToolChannel, ChunkSink]:
        """Приёмники вызова: свой буфер плюс журнал каналов, если тап поставлен."""
        sinks: dict[ToolChannel, ChunkSink] = dict(own)

        journal = ToolChannelsTap.get()
        if journal is None:
            return sinks

        channels = (ToolChannel.STDOUT, ToolChannel.STDERR, ToolChannel.RESULT)
        for channel in channels:
            journal_sink = journal.sink_of(channel).feed
            if channel in sinks:
                sinks[channel] = _Tee(sinks[channel], journal_sink).feed
            else:
                sinks[channel] = journal_sink

        return sinks

    def _run(
        self,
        argv: Sequence[str],
        stdin: bytes,
        own: Mapping[ToolChannel, ChunkSink],
        *,
        with_result: bool,
    ) -> _ProcRun:
        sinks = self._sinks(own)

        env = dict(os.environ)

        result_r = -1
        result_w = -1
        pass_fds: tuple[int, ...] = ()
        if with_result:
            result_r, result_w = os.pipe()
            os.set_inheritable(result_w, True)
            env[ToolChannel.RESULT.env_name] = str(result_w)
            pass_fds = (result_w,)

        started = time.monotonic()
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv собран контрактом модуля
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cfg.workdir,
                env=env,
                pass_fds=pass_fds,
                start_new_session=True,
            )
        except OSError as exc:
            if with_result:
                os.close(result_r)
                os.close(result_w)

            msg = f"{self._tool}: cannot spawn {argv[0]}: {exc}"
            raise ProcessCallError(msg) from exc

        spawn_ms = int((time.monotonic() - started) * 1000)

        # копия записи родителя закрывается сразу: EOF канала результата
        # наступает вместе со смертью тела
        if with_result:
            os.close(result_w)

        writer = threading.Thread(
            target=self._feed_stdin,
            args=(proc, stdin),
            name=f"tool-stdin:{self._tool}",
        )
        writer.start()

        try:
            outcome = self._pump(proc, sinks, started, result_r)
        except BaseException:
            # сорвался приёмник или пришла отмена: тело добивается группой,
            # иначе оно переживёт вызов и продолжит писать в закрытые пайпы
            self._kill(proc)
            proc.wait()
            raise
        finally:
            if with_result:
                os.close(result_r)

            # писатель stdin выходит по EOF пайпа после смерти тела, поэтому
            # join идёт до закрытия пайпов — гонки с писателем нет
            writer.join()
            self._close_pipes(proc)

        return _ProcRun(
            exit_code=outcome.exit_code,
            timed_out=outcome.timed_out,
            duration_ms=int((time.monotonic() - started) * 1000),
            spawn_ms=spawn_ms,
            first_output_ms=outcome.first_output_ms,
        )

    def _pump(
        self,
        proc: subprocess.Popen[bytes],
        sinks: Mapping[ToolChannel, ChunkSink],
        started: float,
        result_r: int,
    ) -> _PumpEnd:
        """Читает каналы до EOF и выхода тела; следит за дедлайном и отменой."""
        sel = selectors.DefaultSelector()
        open_reads = self._register(sel, proc, result_r)

        deadline = started + self._cfg.timeout_sec
        timed_out = False
        first_output: float | None = None

        cancellation = current_cancellation()

        def kill() -> None:
            self._kill(proc)

        with cancellation.abort_with(kill):
            while open_reads or proc.poll() is None:
                if cancellation.cancelled:
                    self._kill(proc)

                if not timed_out and time.monotonic() >= deadline:
                    timed_out = True
                    self._kill(proc)

                got = self._step(sel, sinks, open_reads)
                if got and first_output is None:
                    first_output = time.monotonic()

        sel.close()
        cancellation.raise_if_cancelled()

        first_output_ms: int | None = None
        if first_output is not None:
            first_output_ms = int((first_output - started) * 1000)

        return _PumpEnd(
            exit_code=proc.wait(),
            timed_out=timed_out,
            first_output_ms=first_output_ms,
        )

    @staticmethod
    def _register(
        sel: selectors.BaseSelector,
        proc: subprocess.Popen[bytes],
        result_r: int,
    ) -> set[int]:
        """Регистрирует читаемые каналы тела; возвращает их дескрипторы."""
        if proc.stdout is not None:
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            sel.register(fd, selectors.EVENT_READ, ToolChannel.STDOUT)

        if proc.stderr is not None:
            fd = proc.stderr.fileno()
            os.set_blocking(fd, False)
            sel.register(fd, selectors.EVENT_READ, ToolChannel.STDERR)

        if result_r >= 0:
            os.set_blocking(result_r, False)
            sel.register(result_r, selectors.EVENT_READ, ToolChannel.RESULT)

        return {key.fd for key in sel.get_map().values()}

    def _step(
        self,
        sel: selectors.BaseSelector,
        sinks: Mapping[ToolChannel, ChunkSink],
        open_reads: set[int],
    ) -> bool:
        """Один шаг насоса: читает готовые каналы, True — тело что-то вывело."""
        got = False

        for key, _ in sel.select(timeout=self.POLL_SEC):
            chunk = os.read(key.fd, self.READ_BYTES)
            if not chunk:
                sel.unregister(key.fd)
                open_reads.discard(key.fd)
                continue

            got = True
            sink = sinks.get(key.data)
            if sink is not None:
                sink(chunk)

        return got

    def _feed_stdin(self, proc: subprocess.Popen[bytes], stdin: bytes) -> None:
        if proc.stdin is None:
            return

        # тело умерло до конца записи: причина видна по коду возврата и stderr
        with suppress(BrokenPipeError, OSError):
            proc.stdin.write(stdin)
            proc.stdin.close()

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
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue

            with suppress(OSError):
                stream.close()
