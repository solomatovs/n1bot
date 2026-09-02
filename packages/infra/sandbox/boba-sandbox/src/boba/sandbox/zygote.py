"""Хост-сторона зиготы: супервизор, спавнер из профиля и мост ToolLauncher.

Зигота — резидентный процесс секции инструментов внутри песочницы: она
один раз импортирует тяжёлые модули и на каждый вызов форкает исполнителя
(гостевая сторона — boba.sandbox.guest). Так вызов не платит за холодный
импорт. Здесь живёт хостовая половина: ZygoteSupervisor держит зиготу
живой (поднимает при старте, перезапускает после смерти, сдаётся после
исчерпания попыток), ZygoteSpawner собирает её команду из профиля
песочницы, ZygoteToolCaller реализует протокол ToolLauncher поверх
супервизора. Вызов при неготовой зиготе — ошибка инструмента, а не тихая
деградация.

Ошибки:
ZygoteStartError — зигота не поднялась за отведённые попытки.
ZygoteUnavailableError — вызов пришёл, когда зигота не готова.
ZygoteCallError — зигота умерла посреди вызова, нарушила протокол либо не
    отдала конверт tool_result.
ChannelOverflowError — канал вызова превысил байтовый потолок, вызов убит.
SandboxChainError — исполнитель сообщил, что корень секции не смонтирован.
SandboxMountError — исполнитель сообщил, что образ вызова не смонтирован.
Все они — LauncherError: наружу слой ничего сверх контракта порта не выпускает.
"""

from __future__ import annotations

import array
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation
from boba.sandbox.argv import build_zygote_argv
from boba.sandbox.cgroup import CgroupManager, GroupLimits
from boba.sandbox.diagnostics import SandboxDiagnostics
from boba.sandbox.fds import FdReader
from boba.sandbox.guest import (
    CallExit,
    CallKind,
    CallMounts,
    CallRequest,
    CallSetupFailed,
    ChildLimits,
    ControlMark,
    ImageMount,
    ImageMounting,
    SetupFailure,
    WarmupCall,
    WarmupMessage,
    ZygoteArgs,
    ZygoteWire,
)
from boba.sandbox.profile import SandboxLayout, SandboxMount, SandboxProfile
from boba.sandbox.runner import (
    DeathReport,
    FailureLog,
    IncidentReason,
    LifecycleJournal,
    SandboxChainError,
    SandboxLogRelay,
    SandboxMountError,
    StderrTee,
)
from boba.toolkit.chain import TappedCall
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
    CallInput,
    CallSinks,
    ChannelPump,
    OpenRun,
    PipePlumbing,
    PumpedCall,
)
from boba.toolkit.stream import (
    ChannelSinks,
    ChunkSink,
    ToolChannelsTap,
)
from boba.workspace.binaries import SandboxBinary
from boba.workspace.launcher import (
    LauncherMode,
    ResourceLimits,
    build_chain_argv,
    require_fuse,
)

__all__ = [
    "ZygoteCallError",
    "ZygoteOutcome",
    "ZygotePolicy",
    "ZygoteRegistry",
    "ZygoteSpawner",
    "ZygoteStartError",
    "ZygoteState",
    "ZygoteSupervisor",
    "ZygoteToolCaller",
    "ZygoteUnavailableError",
]

logger = logging.getLogger(__name__)

Spawner = Callable[[int], subprocess.Popen[bytes]]
"""fd child-конца сокета -> запущенный процесс зиготы (bwrap либо python)."""


class ZygoteStartError(LauncherError):
    """Зигота не поднялась: попытки старта исчерпаны."""


class ZygoteUnavailableError(LauncherError):
    """Вызов при неготовой зиготе: инструмент отвечает ошибкой."""


class ZygoteCallError(LauncherError):
    """Зигота умерла посреди вызова, нарушила протокол или не дала конверт."""


class ZygoteState(StrEnum):
    """Состояние супервизора."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"


class ZygotePolicy(BaseModel):
    """Секция конфига [sandbox.zygote]: сколько супервизор ждёт готовности
    зиготы, сколько раз пробует поднять снова и когда считает её здоровой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_timeout_sec: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Сколько секунд ждать готовности зиготы: отсчёт идёт от запуска "
            "процесса до сообщения ready, которое зигота шлёт после импорта "
            "своих модулей и прогрева. Не уложилась — процесс убивается, а "
            "попытка засчитывается как неудачная. Ориентир: холодный импорт "
            "тяжёлой секции (kb с моделью эмбеддингов) занимает единицы секунд."
        ),
    )
    max_start_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Сколько неудачных попыток старта подряд допускается. Когда они "
            "исчерпаны, супервизор прекращает поднимать зиготу и переводит её "
            "в состояние failed: инструменты этой секции отвечают ошибкой, "
            "пока приложение не перезапустят. Без потолка сломанная секция "
            "(ошибка импорта, битый конфиг прогрева) перезапускалась бы вечно."
        ),
    )
    restart_backoff_sec: float = Field(
        default=1.0,
        ge=0,
        description=(
            "Пауза между попытками старта. Зигота, падающая сразу после "
            "запуска, без паузы крутила бы перезапуск десятки раз в секунду и "
            "занимала бы процессор вместо того, чтобы дать администратору "
            "увидеть причину в логе."
        ),
    )
    stop_wait_sec: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Сколько секунд ждать, пока зигота выйдет сама после закрытия "
            "сокета, прежде чем добить её сигналом. Штатный выход занимает "
            "миллисекунды; ожидание нужно на случай, когда зигота доживает "
            "последний вызов."
        ),
    )
    call_poll_sec: float = Field(
        default=0.01,
        gt=0,
        description=(
            "Шаг опроса дескрипторов вызова на стороне приложения: как часто "
            "насос просыпается, если ни один канал не отдал данных. Определяет "
            "точность срабатывания таймаута вызова."
        ),
    )
    healthy_after_sec: float = Field(
        default=30.0,
        ge=0,
        description=(
            "Сколько секунд зигота должна прожить после ready, чтобы её "
            "последующая смерть считалась единичным сбоем: счётчик неудачных "
            "попыток обнуляется, и супервизор поднимает её заново с полным "
            "запасом попыток. Смерть раньше этого порога засчитывается как "
            "неудачная попытка старта. Без такого правила зигота, прожившая "
            "сутки и убитая OOM-killer'ом, тратила бы попытку навсегда, и "
            "через несколько таких смертей здоровая секция осталась бы failed."
        ),
    )


class ZygoteOutcome(BaseModel):
    """Итог одного вызова через зиготу: код выхода исполнителя, длительность,
    таймаут и отчёт о сорванной подготовке (если тело не запустилось).
    Собирается насосом _ZygotePump; в ToolOutcome его превращает
    ZygoteToolCaller.outcome_of."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    duration_ms: int
    timed_out: bool
    child_pid: int
    """Host-pid исполнителя; 0 — исполнитель не родился."""
    setup_failure: SetupFailure = SetupFailure.NONE
    """Чем сорвалась подготовка вызова; NONE — тело было запущено."""
    setup_detail: str = ""


class _CallChannels:
    """Все дескрипторы одного вызова, которые супервизор шлёт зиготе через
    SCM_RIGHTS: пайпы stdin/stdout/stderr/result/frames/injected, control-
    сокет и каталог cgroup-leaf'а.

    Порядок в child_fds() жёсткий — гость раскладывает их по CallFd. После
    отправки child-концы закрываются здесь, host-концы разбирают владельцы:
    stdin забирает CallInput (take_stdin), канал конфига — писатель конфига
    (take_injected), остальное читает насос и закрывает close_host_ends.
    """

    def __init__(self, cgroup_fd: int = -1) -> None:
        self.cgroup_fd = cgroup_fd
        self.stdin_r, self.stdin_w = os.pipe()
        self.stdout_r, self.stdout_w = os.pipe()
        self.stderr_r, self.stderr_w = os.pipe()
        self.result_r, self.result_w = os.pipe()
        self.frames_r, self.frames_w = os.pipe()
        self.injected_r, self.injected_w = os.pipe()
        PipePlumbing.widen(self.stdin_w)
        PipePlumbing.widen(self.frames_w)
        self.control_host, self.control_child = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET
        )
        self.control_host.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self._stdin_open = True
        self._injected_open = True
        self._frames_open = True

    def child_fds(self) -> list[int]:
        """В порядке CallFd: так их ждёт зигота; cgroup — последним и не всегда."""
        listed = [
            self.stdin_r,
            self.stdout_w,
            self.stderr_w,
            self.result_w,
            self.frames_w,
            self.injected_r,
            self.control_child.fileno(),
        ]
        if self.cgroup_fd >= 0:
            listed.append(self.cgroup_fd)

        return listed

    def close_child_ends(self) -> None:
        os.close(self.stdin_r)
        os.close(self.stdout_w)
        os.close(self.stderr_w)
        os.close(self.result_w)
        os.close(self.frames_w)
        os.close(self.injected_r)
        self.control_child.close()

    def stdin_alive(self) -> bool:
        return self._stdin_open

    def take_stdin(self) -> int:
        """Отдать stdin_w владельцу входа: каналы его больше не закрывают."""
        if not self._stdin_open:
            msg = "call stdin is already taken or closed"
            raise LauncherError(msg)

        self._stdin_open = False
        return self.stdin_w

    def take_injected(self) -> int:
        """Отдать канал конфига писателю: каналы его больше не закрывают."""
        if not self._injected_open:
            msg = "call injected channel is already taken or closed"
            raise LauncherError(msg)

        self._injected_open = False
        return self.injected_w

    def take_frames(self) -> int:
        """Отдать канал кадров перекачке: насос его не читает, каналы его
        больше не закрывают; владеет дескриптором перекачка."""
        if not self._frames_open:
            msg = "call frames channel is already taken or closed"
            raise LauncherError(msg)

        self._frames_open = False
        return self.frames_r

    def host_reads(self) -> tuple[tuple[ToolChannel, int], ...]:
        """Читаемые насосом каналы вызова; отданный перекачке не входит."""
        reads: list[tuple[ToolChannel, int]] = [
            (ToolChannel.STDOUT, self.stdout_r),
            (ToolChannel.STDERR, self.stderr_r),
            (ToolChannel.RESULT, self.result_r),
        ]

        if self._frames_open:
            reads.append((ToolChannel.FRAMES, self.frames_r))

        return tuple(reads)

    def close_stdin(self) -> None:
        if not self._stdin_open:
            return

        self._stdin_open = False
        os.close(self.stdin_w)

    def close_host_ends(self) -> None:
        self.close_stdin()
        self._close_injected()
        self._close_frames()
        os.close(self.stdout_r)
        os.close(self.stderr_r)
        os.close(self.result_r)
        self.control_host.close()

    def _close_injected(self) -> None:
        if not self._injected_open:
            return

        self._injected_open = False
        os.close(self.injected_w)

    def _close_frames(self) -> None:
        if not self._frames_open:
            return

        self._frames_open = False
        os.close(self.frames_r)


class ZygoteSupervisor:
    """Держит процесс-зиготу секции живым и проводит через него вызовы.

    Жизненный цикл: start() поднимает зиготу (с попытками и таймаутом из
    ZygotePolicy), монитор-тред перезапускает её после внезапной смерти,
    stop() гасит. Вызов идёт в два шага: begin() шлёт запрос и каналы через
    socketpair (поток вызывающего), run_wired() качает каналы насосом до
    выхода исполнителя (поток прогона); call() соединяет оба шага для
    вызова с заранее известным входом (shell, тесты). ZygoteToolCaller
    строит поверх этого протокол ToolLauncher.
    """

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        spawner: Spawner,
        policy: ZygotePolicy,
        stderr_tail_bytes: int,
        warmup_calls: Sequence[WarmupCall] = (),
        modules: Sequence[str] = (),
        root: str = "",
    ) -> None:
        self._name = name
        self._spawner = spawner
        self._policy = policy
        self._warmup = WarmupMessage(calls=tuple(warmup_calls))
        self._modules = tuple(modules)
        self._root = root
        self._journal = LifecycleJournal(name)
        self._tail_bytes = stderr_tail_bytes
        self._stderr = ChannelTail(stderr_tail_bytes)
        self._calls_total = 0
        self._in_flight: dict[str, float] = {}
        self._born = 0.0
        self._chain_lost = ""

        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._send_lock = threading.Lock()
        self._state = ZygoteState.STOPPED
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._attempts = 0
        self._monitor: threading.Thread | None = None
        self._spawns = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"zygote-spawn-{name}"
        )

    @property
    def state(self) -> ZygoteState:
        return self._state

    @property
    def pid(self) -> int:
        """Host-pid живой зиготы; 0 — процесса сейчас нет."""
        proc = self._proc
        if proc is None:
            return 0

        return proc.pid

    def _report(self, proc: subprocess.Popen[bytes] | None) -> DeathReport:
        """Снимок зиготы для журнала: код выхода, время жизни, счётчики, хвост."""
        pid = 0
        code: int | None = None
        if proc is not None:
            pid = proc.pid
            code = proc.returncode

        uptime = 0.0
        if self._born:
            uptime = time.monotonic() - self._born

        return DeathReport(
            pid=pid,
            exit_code=code,
            uptime_sec=uptime,
            calls_total=self._calls_total,
            in_flight=tuple(sorted(self._in_flight)),
            stderr_tail=self._stderr.text().strip(),
        )

    def _pump_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        """Stderr зиготы в журнал приложения плюс хвост для отчёта о смерти.

        Без этого последние слова умершей зиготы (и кадры цепочки запуска)
        уходили в stderr приложения без всякой привязки к секции.
        """
        stream = proc.stderr
        if stream is None:
            return

        relay = SandboxLogRelay(self._name, _RelayTee(None, self._stderr))
        reader = FdReader(stream.fileno())

        def pump() -> None:
            while True:
                chunk = reader.read()
                if not chunk:
                    break

                relay.feed(chunk)

            relay.flush()

        threading.Thread(
            target=pump, name=f"zygote-stderr-{self._name}", daemon=True
        ).start()

    def chain_lost(self, detail: str) -> None:
        """Корень секции отвалился: зигота гасится, монитор поднимает её заново.

        Пока корень мёртв, любой вызов падает на монтировании и повторять его
        бессмысленно, поэтому секция перезапускается целиком.
        """
        with self._lock:
            proc = self._proc
            if proc is None or self._chain_lost:
                return

            self._chain_lost = detail

        logger.warning(
            "zygote[%s]: root mount is gone (%s), restarting the section",
            self._name,
            detail.strip(),
        )
        proc.kill()

    def start(self) -> None:
        """Первый подъём; неудача всех попыток — ZygoteStartError.

        Подъём идёт один: пришедшие следом ждут его исхода. Иначе вызов из
        второго потока уходил бы в ещё не готовую зиготу.
        """
        with self._lock:
            if self._state is ZygoteState.READY:
                return

            if self._state is ZygoteState.STARTING:
                self._await_start()
                return

            self._state = ZygoteState.STARTING
            self._attempts = 0

        self._journal.open(IncidentReason.START, f"modules={len(self._modules)}")

        while True:
            self._journal.attempt(
                self._attempts + 1,
                self._policy.max_start_attempts,
                backoff_sec=0.0,
            )
            if self._try_start():
                self._watch()
                return

            with self._settled:
                self._attempts += 1
                if self._attempts >= self._policy.max_start_attempts:
                    self._state = ZygoteState.FAILED
                    self._settled.notify_all()
                    self._journal.gave_up(self._attempts)
                    msg = (
                        f"zygote {self._name}: not ready after "
                        f"{self._attempts} attempt(s)"
                    )
                    raise ZygoteStartError(msg)

            time.sleep(self._policy.restart_backoff_sec)

    def _await_start(self) -> None:
        """Ждёт исхода чужого подъёма; вызывается под уже взятым локом."""
        limit = self._policy.start_timeout_sec * self._policy.max_start_attempts
        deadline = time.monotonic() + limit

        while self._state is ZygoteState.STARTING:
            left = deadline - time.monotonic()
            if left <= 0:
                break

            self._settled.wait(left)

        if self._state is ZygoteState.READY:
            return

        msg = f"zygote {self._name}: start by another caller ended as {self._state}"
        raise ZygoteStartError(msg)

    def stop(self) -> None:
        with self._settled:
            self._state = ZygoteState.STOPPED
            self._settled.notify_all()
            proc = self._proc
            sock = self._sock
            self._proc = None
            self._sock = None

        if sock is not None:
            sock.close()

        if proc is None:
            self._spawns.shutdown(wait=False)
            return

        try:
            proc.wait(timeout=self._policy.stop_wait_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        self._journal.stopped(self._report(proc))
        self._spawns.shutdown(wait=False)

    def begin(  # noqa: PLR0913
        self,
        call_id: str,
        argv: Sequence[str],
        limits: ChildLimits,
        *,
        isolate: bool,
        mounts: CallMounts,
        cgroup_leaf: str = "",
        images: Sequence[ImageMount] = (),
        mounting: ImageMounting | None = None,
        staging: Sequence[str] = (),
        cwd: str = "",
        kind: CallKind = CallKind.MODULE,
    ) -> _WiredCall:
        """Открыть проводку вызова: каналы и запрос зиготе, без насоса.

        Идёт в потоке вызывающего: после возврата stdin вызова готов к
        прямой записи, а run_wired качает каналы до выхода исполнителя.
        """
        with self._lock:
            sock = self._sock
            if self._state is not ZygoteState.READY or sock is None:
                msg = f"zygote {self._name} is not ready: {self._state}"
                raise ZygoteUnavailableError(msg)

        with self._lock:
            self._calls_total += 1
            self._in_flight[call_id] = time.monotonic()

        cgroup_fd = -1
        if cgroup_leaf:
            cgroup_fd = os.open(cgroup_leaf, os.O_RDONLY | os.O_DIRECTORY)

        channels = _CallChannels(cgroup_fd)
        request = CallRequest(
            call_id=call_id,
            kind=kind,
            argv=tuple(argv),
            limits=limits,
            isolate=isolate,
            mounts=mounts,
            images=tuple(images),
            mounting=mounting,
            staging=tuple(staging),
            cwd=cwd,
            into_cgroup=cgroup_fd >= 0,
        )

        try:
            with self._send_lock:
                ZygoteWire.send(sock, request, channels.child_fds())
        except OSError as exc:
            channels.close_child_ends()
            channels.close_host_ends()
            with self._lock:
                self._in_flight.pop(call_id, None)
            msg = f"zygote {self._name}: request not sent: {exc}"
            raise ZygoteUnavailableError(msg) from exc

        channels.close_child_ends()
        if cgroup_fd >= 0:
            os.close(cgroup_fd)

        return _WiredCall(request=request, channels=channels)

    def run_wired(
        self,
        wired: _WiredCall,
        sinks: Mapping[ToolChannel, ChunkSink],
        *,
        timeout_sec: float,
        kill_grace_sec: float,
        cancellation: RunCancellation,
    ) -> ZygoteOutcome:
        """Качать каналы открытого вызова до выхода исполнителя."""
        pump = _ZygotePump(
            name=self._name,
            request=wired.request,
            channels=wired.channels,
            sinks=sinks,
            timeout_sec=timeout_sec,
            poll_sec=self._policy.call_poll_sec,
        )

        try:
            return pump.run_call(cancellation)
        except BaseException:
            # сорвался приёмник вывода: без добивания исполнитель и его
            # fuse2fs остались бы жить и держать образ пользователя
            pump.abort(kill_grace_sec)
            raise
        finally:
            pump.close()
            wired.channels.close_host_ends()
            with self._lock:
                self._in_flight.pop(wired.request.call_id, None)

    def abandon_wired(self, wired: _WiredCall) -> None:
        """Прибрать проводку, чей насос так и не родился; повтор безвреден."""
        wired.channels.close_host_ends()

        with self._lock:
            self._in_flight.pop(wired.request.call_id, None)

    def call(  # noqa: PLR0913
        self,
        call_id: str,
        argv: Sequence[str],
        stdin: bytes,
        config: bytes,
        limits: ChildLimits,
        sinks: Mapping[ToolChannel, ChunkSink],
        *,
        isolate: bool,
        mounts: CallMounts,
        timeout_sec: float,
        kill_grace_sec: float,
        cgroup_leaf: str = "",
        images: Sequence[ImageMount] = (),
        mounting: ImageMounting | None = None,
        staging: Sequence[str] = (),
        cwd: str = "",
        kind: CallKind = CallKind.MODULE,
    ) -> ZygoteOutcome:
        """Вызов с заранее известным входом: проводка, stdin и конфиг, насос."""
        wired = self.begin(
            call_id,
            argv,
            limits,
            isolate=isolate,
            mounts=mounts,
            cgroup_leaf=cgroup_leaf,
            images=images,
            mounting=mounting,
            staging=staging,
            cwd=cwd,
            kind=kind,
        )

        entry = CallInput(wired.channels.take_stdin())
        config_input = CallInput(wired.channels.take_injected())

        def pump_run(cancellation: RunCancellation) -> ZygoteOutcome:
            return self.run_wired(
                wired,
                sinks,
                timeout_sec=timeout_sec,
                kill_grace_sec=kill_grace_sec,
                cancellation=cancellation,
            )

        try:
            opened = OpenRun(self._name, entry, pump_run)
        except BaseException:
            # ход уже отменён: насос не родился, проводку прибираем сами
            entry.abandon()
            config_input.abandon()
            self.abandon_wired(wired)
            raise

        config_input.send_bytes(config)
        config_input.finish()

        entry.send_bytes(stdin)
        entry.finish()

        return opened.wait()

    def _try_start(self) -> bool:
        """Запуск идёт на своём треде супервизора.

        pdeathsig, которым держится `--die-with-parent`, срабатывает на смерть
        треда-родителя, а не процесса: зигота, поднятая из временного треда
        (пул, обработчик запроса), умирала бы вместе с ним.
        """
        return self._spawns.submit(self._spawn_once).result()

    def _spawn_once(self) -> bool:
        host_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)

        try:
            os.set_inheritable(child_sock.fileno(), True)
            proc = self._spawner(child_sock.fileno())
        except OSError as exc:
            host_sock.close()
            child_sock.close()
            self._journal.failed(f"spawn failed: {exc}", self._stderr.text().strip())
            return False

        child_sock.close()

        self._stderr = ChannelTail(self._tail_bytes)
        self._pump_stderr(proc)
        self._journal.spawned(proc.pid, self._root, self._modules)

        host_sock.settimeout(self._policy.start_timeout_sec)
        try:
            ZygoteWire.send(host_sock, self._warmup)
            message, _fds = ZygoteWire.recv(host_sock)
        except (TimeoutError, OSError) as exc:
            tail = self._stderr.text().strip()
            self._journal.failed(f"no ready message: {exc}", tail)
            self._abandon(proc, host_sock)
            return False

        if message.get("op") != "ready":
            self._journal.failed(
                f"unexpected hello: {message}", self._stderr.text().strip()
            )
            self._abandon(proc, host_sock)
            return False

        host_sock.settimeout(None)

        with self._settled:
            self._proc = proc
            self._sock = host_sock
            self._state = ZygoteState.READY
            self._born = time.monotonic()
            self._chain_lost = ""
            self._settled.notify_all()

        warmup = message.get("warmup_ms")
        if not isinstance(warmup, int):
            warmup = 0

        self._journal.ready(proc.pid, warmup)
        return True

    def _abandon(self, proc: subprocess.Popen[bytes], sock: socket.socket) -> None:
        sock.close()
        proc.kill()
        proc.wait()

    def _watch(self) -> None:
        """Монитор-тред: перезапуск после внезапной смерти, учёт попыток."""
        with self._lock:
            proc = self._proc

        if proc is None:
            return

        def monitor(watched: subprocess.Popen[bytes]) -> None:
            born = time.monotonic()
            watched.wait()

            with self._lock:
                if self._state is ZygoteState.STOPPED:
                    return

                if self._proc is not watched:
                    return

                self._proc = None
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None

                lived = time.monotonic() - born
                if lived >= self._policy.healthy_after_sec:
                    self._attempts = 0

                self._state = ZygoteState.STARTING

            reason = IncidentReason.DIED
            detail = ""
            if self._chain_lost:
                reason = IncidentReason.CHAIN_LOST
                detail = self._chain_lost

            self._journal.open(reason, detail)
            self._journal.death(self._report(watched))

            self._restart_loop()

        self._monitor = threading.Thread(
            target=monitor, args=(proc,), name=f"zygote-{self._name}", daemon=True
        )
        self._monitor.start()

    def _restart_loop(self) -> None:
        while True:
            time.sleep(self._policy.restart_backoff_sec)

            with self._lock:
                if self._state is ZygoteState.STOPPED:
                    return

            self._journal.attempt(
                self._attempts + 1,
                self._policy.max_start_attempts,
                backoff_sec=self._policy.restart_backoff_sec,
            )
            if self._try_start():
                self._watch()
                return

            with self._settled:
                self._attempts += 1
                if self._attempts >= self._policy.max_start_attempts:
                    self._state = ZygoteState.FAILED
                    self._settled.notify_all()
                    self._journal.gave_up(self._attempts)
                    return


@dataclass(frozen=True)
class _WiredCall:
    """Проводка открытого вызова: запрос уже уехал зиготе, host-концы
    каналов ещё у нас. Отдаётся из begin() и живёт до конца run_wired()
    либо до abandon_wired(), если насос так и не родился."""

    request: CallRequest
    channels: _CallChannels


class _ZygotePump(ChannelPump):
    """Реализация ChannelPump для вызова через зиготу: каналы тела плюс
    control-события исполнителя.

    Завершение исполнителя здесь определяет не poll (процесс форкает
    зигота, у хоста его нет), а control-сокет: событие born несёт host-pid
    исполнителя, exit — код выхода; EOF сокета до exit означает смерть
    зиготы посреди вызова. Вход тела насос не пишет: им владеет вызывающий
    через CallInput.
    """

    def __init__(  # noqa: PLR0913 — насос держит все части одного вызова
        self,
        name: str,
        request: CallRequest,
        channels: _CallChannels,
        sinks: Mapping[ToolChannel, ChunkSink],
        timeout_sec: float,
        poll_sec: float,
    ) -> None:
        super().__init__(poll_sec, timeout_sec)
        self._name = name
        self._request = request
        self._channels = channels
        self._opened = time.monotonic()

        self._child_pid = 0
        self._exit_code: int | None = None
        self._setup_failure = SetupFailure.NONE
        self._setup_detail = ""

        for channel, fd in channels.host_reads():
            sink = sinks.get(channel)
            if sink is None:
                self.add_drain(fd)
                continue

            self.add_read(fd, sink)

        self.add_event(channels.control_host.fileno(), self._control_event)

    def run_call(self, cancellation: RunCancellation) -> ZygoteOutcome:
        """Прогнать вызов и собрать его итог."""
        end = self.run(cancellation)

        return self._outcome(end.timed_out)

    def _finished(self) -> bool:
        return self._exit_code is not None

    def _quit_on_timeout(self) -> bool:
        """После таймаута ждать нечего, только если исполнитель не родился."""
        return self._child_pid == 0

    def _kill(self) -> None:
        if self._child_pid:
            _kill_quietly(self._child_pid)

    def _control_event(self) -> None:
        """born с host-pid исполнителя либо exit с кодом; EOF — смерть зиготы."""
        space = socket.CMSG_SPACE(3 * array.array("i").itemsize)
        data, ancdata, _flags, _addr = self._channels.control_host.recvmsg(1024, space)

        if not data:
            msg = f"zygote {self._name}: control closed on call {self._request.call_id}"
            raise ZygoteCallError(msg)

        if data == ControlMark.BORN.bytes():
            self._child_pid = self._creds_pid(ancdata)
            return

        if self._setup_failed(data):
            return

        exit_message = CallExit.model_validate_json(data)
        self._exit_code = exit_message.code
        self.drop_event(self._channels.control_host.fileno())

    def _setup_failed(self, data: bytes) -> bool:
        """Отчёт исполнителя о сорванной подготовке: тело до него не дожило."""
        try:
            report = CallSetupFailed.model_validate_json(data)
        except ValueError:
            return False

        self._setup_failure = report.reason
        self._setup_detail = report.detail
        return True

    @staticmethod
    def _creds_pid(ancdata: list[tuple[int, int, bytes]]) -> int:
        for level, kind, blob in ancdata:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                creds = array.array("i")
                creds.frombytes(blob[: 3 * creds.itemsize])
                return creds[0]

        msg = "zygote handshake: born without SCM_CREDENTIALS"
        raise ZygoteCallError(msg)

    def _outcome(self, timed_out: bool) -> ZygoteOutcome:
        exit_code = self._exit_code

        if exit_code is None:
            if not timed_out:
                msg = f"zygote {self._name}: died mid-call {self._request.call_id}"
                raise ZygoteCallError(msg)

            exit_code = -int(signal.SIGKILL)

        return ZygoteOutcome(
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - self._opened) * 1000),
            timed_out=timed_out,
            child_pid=self._child_pid,
            setup_failure=self._setup_failure,
            setup_detail=self._setup_detail,
        )


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class ZygoteMounts:
    """Считает пути обвязки монтирования образов внутри зиготы (шаблон,
    fuse2fs, каталог образов) по фиксированным точкам SandboxLayout и
    проверяет их предпосылки на хосте."""

    def __init__(self, profile: SandboxProfile) -> None:
        self._profile = profile

    def check(self) -> None:
        """Предпосылки монтирования образов: бинарь есть, каталог образов есть."""
        self._profile.host.binaries.resolve(SandboxBinary.FUSE2FS)

        workspace = self._profile.mounts.workspace
        if workspace is None:
            return

        self._check_directory(workspace.images_dir())

    def template(self) -> str:
        """Эталон образа внутри песочницы."""
        return SandboxMount.SETUP_TEMPLATE.value

    def fuse2fs_dir(self) -> str:
        return os.path.dirname(SandboxMount.SETUP_FUSE2FS.value)

    def image(self, host: str) -> str:
        """Образ пользователя внутри песочницы по его хостовому пути."""
        return SandboxLayout.image_inside(host)

    def staging(self) -> tuple[str, ...]:
        """Что исполнитель отцепит после монтирования: точки обвязки.

        Шаблон образа, бинарь fuse2fs и каталог образов пользователей нужны
        только ради монтирования, и телу инструмента они не видны.
        """
        if self._profile.mounts.workspace is None:
            return ()

        return (SandboxMount.SETUP.value,)

    @staticmethod
    def _check_directory(directory: str) -> None:
        if "{" in directory:
            return

        if Path(directory).is_dir():
            return

        msg = f"zygote: images directory {directory!r} does not exist on the host"
        raise ZygoteStartError(msg)


class ZygoteSpawner:
    """Собирает и запускает процесс зиготы из профиля песочницы; его spawn
    супервизор зовёт на каждом подъёме секции.

    Корень образом монтируется цепочкой лаунчера один раз на жизнь зиготы:
    внешний bwrap -> лаунчер (fuse2fs) -> вложенный bwrap с корнем из
    монтирования -> зигота. Корень каталогом идёт прямым bwrap. tmpfs на
    /tmp обязателен — из него дети получают приватный размерный /tmp.
    """

    PYTHON: ClassVar[str] = "python3"
    MODULE: ClassVar[str] = "boba.sandbox.guest"

    APP_LOGGER: ClassVar[str] = "boba"
    """Чей уровень наследует зигота: настройка живёт в конфиге приложения."""

    def __init__(
        self,
        profile: SandboxProfile,
        modules: Sequence[str],
        policy: ZygotePolicy,
    ) -> None:
        self._policy = policy
        # образ workspace рендерится на вызов и монтируется ребёнком: зиготе
        # нужен только его каталог; остальной профиль переменных не имеет
        without_workspace = profile.mounts.model_copy(update={"workspace": None})
        bare = profile.model_copy(update={"mounts": without_workspace})
        try:
            bare = bare.render({})
        except RuntimeError as exc:
            msg = f"zygote profile has per-call path variables: {exc}"
            raise ZygoteStartError(msg) from exc

        restored = bare.mounts.model_copy(
            update={"workspace": profile.mounts.workspace}
        )
        bare = bare.model_copy(update={"mounts": restored})

        self._call_mounts = self.call_mounts(bare)
        self._with_images = profile.mounts.workspace is not None
        self._profile = bare
        self._modules = tuple(modules)

        if self._with_images:
            ZygoteMounts(profile).check()

    def root_label(self) -> str:
        """Чем секции служит корень."""
        return f"image {self._profile.rootfs}"

    def spawn(self, fd: int) -> subprocess.Popen[bytes]:
        env = dict(self._profile.isolation.env)

        args = ZygoteArgs(
            socket_fd=fd,
            reap_poll_sec=self._profile.isolation.reap_poll_sec,
            log_level=self.log_level(),
            modules=self._modules,
        )
        command = [self.PYTHON, "-m", self.MODULE, *args.render()]

        argv = self._argv(command, env, fd)

        proc = subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=(fd,),
            env=dict(os.environ),
        )

        self._pin_cpus(proc.pid)

        return proc

    @classmethod
    def log_level(cls) -> str:
        """Уровень логера приложения: у настройки один источник — его конфиг."""
        level = logging.getLogger(cls.APP_LOGGER).getEffectiveLevel()

        return logging.getLevelName(level)

    def _pin_cpus(self, pid: int) -> None:
        """Маска ядер зиготы по квоте профиля: прогрев греет столько же ядер.

        Дети наследуют маску и правят её сами под свою квоту.
        """
        cores = self._profile.cpu_cores()
        if not cores:
            return

        available = sorted(os.sched_getaffinity(0))
        if len(available) <= cores:
            return

        os.sched_setaffinity(pid, set(available[:cores]))

    def _argv(self, command: list[str], env: Mapping[str, str], fd: int) -> list[str]:
        require_fuse(self._profile.host.binaries)

        # вложенный bwrap стартует уже смонтированным корнем, а не образом
        inner_argv = build_zygote_argv(
            self._profile, command, env=env, root=SandboxMount.ROOTFS.value
        )

        # внешний bwrap держит / хоста read-only: пути записи он обязан знать,
        # иначе вложенный bind не сделает их записываемыми
        setup = self._profile.setup_binds()

        rw_paths: list[str] = []
        for spec in (*self._profile.mounts.rw, *setup.rw):
            rw_paths.append(spec.host)

        return build_chain_argv(
            images=(),
            ro_images=((self._profile.rootfs, SandboxMount.ROOTFS.value),),
            template="",
            op=[LauncherMode.RUN.value, shlex.join(inner_argv)],
            python_bin=sys.executable,
            options=self._profile.host.mounting.to_options(),
            limits=ResourceLimits(),
            binaries=self._profile.host.binaries,
            pass_fds=(fd,),
            rw_paths=rw_paths,
            network=self._profile.isolation.network,
            replace=True,
        )

    @staticmethod
    def call_mounts(profile: SandboxProfile) -> CallMounts:
        """Приватные точки вызова: procfs и /tmp с размером из профиля."""
        return CallMounts(
            proc=SandboxMount.PROC.value,
            tmp=SandboxMount.TMP.value,
            tmp_bytes=profile.mounts.tmp,
        )


class _RelayTee(StderrTee):
    """Наследник StderrTee: сырые строки stderr тела уходят и в журнал
    вызова, и в свой буфер (хвост для диагностики сбоя)."""

    def __init__(
        self, sinks: ChannelSinks | None, own: CappedChannel | ChannelTail
    ) -> None:
        super().__init__(sinks, ToolChannel.STDERR)
        self._own = own

    def raw(self, line: str) -> None:
        super().raw(line)
        self._own.feed(f"{line}\n".encode())


class ZygoteToolCaller(ToolLauncher):
    """Реализация протокола ToolLauncher поверх зиготы: вызовы инструментов
    в bwrap-песочнице.

    open() открывает проводку через ZygoteSupervisor.begin и отдаёт
    PumpedCall; call_text() исполняет shell-команду изолированным ребёнком.
    Сюда же стянута профильная обвязка вызова: лимиты и образы (_plan),
    cgroup-leaf (_acquire_leaf/_release_leaf), диагностика сбоев лимитами
    профиля (_diagnose).
    """

    ARGV_HEAD: ClassVar[int] = 3
    """python3 -m <module> — префикс команды модуля инструментов."""

    def __init__(
        self,
        tool: str,
        supervisor: ZygoteSupervisor,
        profile: SandboxProfile,
        path_vars: Callable[[], Mapping[str, str]] = dict,
    ) -> None:
        self._tool = tool
        self._supervisor = supervisor
        self._profile = profile
        self._path_vars = path_vars
        self._call_mounts = ZygoteSpawner.call_mounts(profile)

    @property
    def supervisor(self) -> ZygoteSupervisor:
        return self._supervisor

    MODULE_JOURNAL: ClassVar[tuple[ToolChannel, ...]] = (
        ToolChannel.STDOUT,
        ToolChannel.RESULT,
        ToolChannel.FRAMES,
    )
    """Каналы вызова модуля для журнального тапа; stderr ведёт релей сам."""

    SHELL_JOURNAL: ClassVar[tuple[ToolChannel, ...]] = (
        ToolChannel.STDOUT,
        ToolChannel.RESULT,
    )
    """Каналы shell-команды для журнального тапа."""

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Shell-команда в изолированном ребёнке: stdout/stderr/rc как есть."""
        limit = self._profile.host.channel_limit_bytes
        stdout = CappedChannel(limit, ToolChannel.STDOUT.value)
        stderr = CappedChannel(limit, ToolChannel.STDERR.value)

        relay = SandboxLogRelay(self._tool, _RelayTee(ToolChannelsTap.get(), stderr))

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.STDERR: relay.feed,
            ToolChannel.STDOUT: stdout.feed,
        }
        sinks = CallSinks.merged(own, self.SHELL_JOURNAL)

        shell = self._profile.run.shell
        if not shell:
            msg = f"{self._tool}: profile declares no shell for text commands"
            raise ZygoteCallError(msg)

        argv = (shell, "-c", command)
        plan = self._plan()

        try:
            outcome = self._grouped_call(
                stdin.encode("utf-8"), argv, sinks, plan, kind=CallKind.SHELL
            )
        finally:
            relay.flush()

        run = RunResult(
            exit_code=outcome.exit_code,
            stdout=stdout.text(),
            stderr=stderr.text(),
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
        )
        self._raise_on_setup_failure(outcome)

        if run.exit_code != 0:
            self._log_failure(run, self._profile.host.fail_tail_chars)

        return LaunchOutcome(self._tool, run, self._diagnose(run, ""))

    def open(self, command: ToolCommand) -> ToolCall:
        """Вызов модуля в песочнице: конфиг первым кадром, кадры тела наружу.

        Проводка и cgroup-leaf готовятся в потоке вызывающего; насос живёт
        в PumpedCall и на любом исходе отпускает leaf и host-концы каналов.
        """
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
        argv_tail = self._argv_tail(command)
        plan = self._plan()

        envelope = CappedChannel(
            self._profile.host.channel_limit_bytes, ToolChannel.RESULT.value
        )
        stderr_tail = ChannelTail(self._profile.host.stderr_tail_bytes)
        inbox = CallInbox()

        relay = SandboxLogRelay(
            self._tool, _RelayTee(ToolChannelsTap.get(), stderr_tail)
        )

        own: dict[ToolChannel, ChunkSink] = {
            ToolChannel.STDERR: relay.feed,
            ToolChannel.RESULT: envelope.feed,
        }
        journal = list(self.MODULE_JOURNAL)

        # сырой канал кадров хост не разбирает и не журналирует: без tap
        # насос дочитывает его в никуда, с tap — отдаёт перекачке
        if not tap and not command.raw_frames:
            own[ToolChannel.FRAMES] = inbox.feed
            journal.append(ToolChannel.FRAMES)

        sinks = CallSinks.merged(own, tuple(journal))

        manager, leaf = self._acquire_leaf()

        try:
            wired = self._supervisor.begin(
                uuid.uuid4().hex,
                argv_tail,
                plan.limits,
                isolate=True,
                mounts=self._call_mounts,
                cgroup_leaf=leaf,
                images=plan.images,
                mounting=plan.mounting,
                staging=plan.staging,
                cwd=plan.cwd,
                kind=CallKind.MODULE,
            )
        except BaseException:
            self._release_leaf(manager, leaf)
            raise

        frames_fd = -1
        if tap:
            frames_fd = wired.channels.take_frames()

        entry = CallSinks.stdin_input(
            wired.channels.take_stdin(), framed=not command.raw_stdin
        )

        def run(cancellation: RunCancellation) -> ZygoteOutcome:
            try:
                return self._supervisor.run_wired(
                    wired,
                    sinks,
                    timeout_sec=plan.timeout_sec,
                    kill_grace_sec=plan.kill_grace_sec,
                    cancellation=cancellation,
                )
            finally:
                relay.flush()
                self._release_leaf(manager, leaf)

        def finish(outcome: ZygoteOutcome) -> ToolOutcome:
            return self.outcome_of(outcome, envelope, stderr_tail)

        try:
            call = PumpedCall(self._tool, entry, inbox, run, finish)
        except BaseException:
            # ход уже отменён: насос не родился, проводку прибираем сами;
            # EOF каналов выведет тело, зигота пожнёт его сама
            entry.abandon()
            if frames_fd >= 0:
                with suppress(OSError):
                    os.close(frames_fd)
            self._supervisor.abandon_wired(wired)
            self._release_leaf(manager, leaf)
            raise

        # насос уже жив: запись конфига блокируется только скоростью тела
        config_input = CallInput(wired.channels.take_injected())
        config_input.send_bytes(command.config)
        config_input.finish()

        return call, frames_fd

    def outcome_of(
        self,
        outcome: ZygoteOutcome,
        envelope: CappedChannel,
        stderr_tail: ChannelTail,
    ) -> ToolOutcome:
        """Итог вызова модуля: диагностика песочницы плюс разбор конверта."""
        run = RunResult(
            exit_code=outcome.exit_code,
            stdout="",
            stderr=stderr_tail.text(),
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
        )
        self._raise_on_setup_failure(outcome)

        if run.exit_code != 0:
            self._log_failure(run, self._profile.host.fail_tail_chars)

        diagnostic = self._diagnose(run, stderr_tail.text())
        reply = EnvelopeReply.parse(self._tool, envelope.data(), run, diagnostic)

        return ToolOutcome(reply=reply, run=run, diagnostic=diagnostic)

    def _log_failure(self, result: RunResult, limit: int) -> None:
        logger.warning("zygote[%s]: %s", self._tool, FailureLog.describe(result, limit))

    def _raise_on_setup_failure(self, outcome: ZygoteOutcome) -> None:
        """Подготовка сорвалась: об этом сказал исполнитель, а не тело.

        Отчёт приходит control-сокетом, которого у тела нет: печать в stderr
        любых меток на решение хоста больше не влияет.
        """
        if outcome.setup_failure is SetupFailure.CHAIN_LOST:
            self._supervisor.chain_lost(outcome.setup_detail)

            msg = (
                f"sandbox: root mount of section {self._tool} is gone: "
                f"{outcome.setup_detail}"
            )
            raise SandboxChainError(msg)

        if outcome.setup_failure is SetupFailure.MOUNT_ERROR:
            msg = f"sandbox: image not mounted: {outcome.setup_detail}"
            raise SandboxMountError(msg)

    def _diagnose(self, result: RunResult, tool_stderr: str) -> str:
        """Объяснение сбоя лимитами профиля; в разборе и хвост tool_stderr."""
        rendered = self._profile.render(dict(self._path_vars()))

        merged = replace(result, stderr=f"{result.stderr}\n{tool_stderr}")

        diagnostic = SandboxDiagnostics.explain(merged, rendered)
        if diagnostic:
            logger.warning("zygote[%s]: %s", self._tool, diagnostic)

        return diagnostic

    def _plan(self) -> _CallPlan:
        """Параметры одного вызова из профиля: лимиты, образы, пути."""
        timeout_sec = self._profile.limits.timeout_sec
        if timeout_sec is None:
            msg = f"zygote {self._tool}: profile without timeout_sec"
            raise ZygoteCallError(msg)

        limits = ChildLimits(
            max_memory_bytes=self._profile.limits.process_memory_bytes,
            max_cpu_sec=self._profile.limits.process_cpu_sec,
            max_file_size_bytes=self._profile.limits.process_file_bytes,
            max_open_files=self._profile.limits.process_open_files,
            oom_score_adj=self._profile.limits.process_oom_score_adj,
            cpu_cores=self._profile.cpu_cores(),
        )

        rendered = self._profile.render(dict(self._path_vars()))
        images = self._images_of(rendered)

        return _CallPlan(
            timeout_sec=float(timeout_sec),
            kill_grace_sec=self._profile.host.kill_grace_sec,
            limits=limits,
            images=images,
            mounting=self._mounting_of(images),
            staging=ZygoteMounts(rendered).staging(),
            cwd=rendered.run.cwd,
        )

    def _acquire_leaf(self) -> tuple[CgroupManager | None, str]:
        """Cgroup-leaf вызова, если профиль требует групповые лимиты."""
        group = GroupLimits.of_profile(self._profile)
        if not group.requested:
            return None, ""

        manager = CgroupManager(self._profile.host.cgroup_base)
        leaf = manager.acquire(group)

        return manager, str(leaf)

    def _release_leaf(self, manager: CgroupManager | None, leaf: str) -> None:
        """Отпустить leaf вызова; отпускается ровно одной стороной."""
        if manager is None:
            return

        leaf_path = Path(leaf)
        if note := manager.throttling(leaf_path):
            logger.warning("zygote[%s]: %s", self._tool, note)

        manager.release(leaf_path)

    def _grouped_call(
        self,
        stdin: bytes,
        argv: tuple[str, ...],
        sinks: Mapping[ToolChannel, ChunkSink],
        plan: _CallPlan,
        *,
        kind: CallKind,
    ) -> ZygoteOutcome:
        """Вызов с готовым входом в собственном cgroup-leaf'е, если он нужен."""
        manager, leaf = self._acquire_leaf()

        try:
            return self._supervisor.call(
                uuid.uuid4().hex,
                argv,
                stdin,
                b"",
                plan.limits,
                sinks,
                isolate=True,
                mounts=self._call_mounts,
                timeout_sec=plan.timeout_sec,
                kill_grace_sec=plan.kill_grace_sec,
                cgroup_leaf=leaf,
                images=plan.images,
                mounting=plan.mounting,
                staging=plan.staging,
                cwd=plan.cwd,
                kind=kind,
            )
        finally:
            self._release_leaf(manager, leaf)

    def _images_of(self, rendered: SandboxProfile) -> tuple[ImageMount, ...]:
        """Образ workspace путями внутри зиготы; путь уже отрендерен профилем."""
        workspace = rendered.mounts.workspace
        if workspace is None:
            return ()

        mounts = ZygoteMounts(rendered)
        image = ImageMount(
            image=mounts.image(workspace.mount.host),
            target=workspace.mount.target,
            template=mounts.template(),
        )

        return (image,)

    def _mounting_of(self, images: Sequence[ImageMount]) -> ImageMounting | None:
        if not images:
            return None

        mounts = ZygoteMounts(self._profile)
        launcher = self._profile.host.mounting
        return ImageMounting(
            fuse2fs_dir=mounts.fuse2fs_dir(),
            mount_wait_sec=launcher.mount_wait_sec,
            mount_poll_sec=launcher.mount_poll_sec,
            shutdown_wait_sec=launcher.shutdown_wait_sec,
            lock_wait_sec=launcher.lock_wait_sec,
            copy_chunk_bytes=launcher.copy_chunk_bytes,
        )

    def _argv_tail(self, command: ToolCommand) -> tuple[str, ...]:
        """Команда модуля без префикса python: имя тула и флаги."""
        argv = command.argv
        if len(argv) <= self.ARGV_HEAD or argv[1] != "-m":
            msg = f"{self._tool}: not a tool module command: {argv[:3]}"
            raise ZygoteCallError(msg)

        return argv[self.ARGV_HEAD :]


@dataclass(frozen=True)
class _CallPlan:
    """Готовые параметры одного вызова, выведенные из профиля секции:
    таймаут, rlimit'ы, образы и рабочий каталог. Считаются один раз в
    _plan() и едут в begin/call супервизора."""

    timeout_sec: float
    kill_grace_sec: float
    limits: ChildLimits
    images: tuple[ImageMount, ...]
    mounting: ImageMounting | None
    staging: tuple[str, ...]
    cwd: str


class ZygoteRegistry:
    """Процессный реестр супервизоров: одна живая зигота на tool-секцию,
    сколько бы раз ни собирались инструменты.

    load_tools зовётся несколько раз (bootstrap, DI): повторный obtain отдаёт
    уже поднятый супервизор. stop_all гасит всех на shutdown приложения.
    """

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _entries: ClassVar[dict[str, ZygoteSupervisor]] = {}

    @classmethod
    def obtain(
        cls,
        name: str,
        profile: SandboxProfile,
        modules: Sequence[str],
        policy: ZygotePolicy,
        warmup_calls: Sequence[WarmupCall] = (),
    ) -> ZygoteSupervisor:
        """Живой супервизор секции; при отсутствии — поднять и запомнить.

        start() зовётся и для найденного супервизора: он идемпотентен и ждёт,
        если зиготу в этот момент поднимает другой поток.
        """
        with cls._lock:
            supervisor = cls._entries.get(name)
            if supervisor is None or supervisor.state is ZygoteState.STOPPED:
                spawner = ZygoteSpawner(profile, modules, policy)
                supervisor = ZygoteSupervisor(
                    name,
                    spawner.spawn,
                    policy,
                    stderr_tail_bytes=profile.host.stderr_tail_bytes,
                    warmup_calls=warmup_calls,
                    modules=modules,
                    root=spawner.root_label(),
                )
                cls._entries[name] = supervisor

        supervisor.start()
        return supervisor

    @classmethod
    def stop_all(cls) -> None:
        with cls._lock:
            entries = list(cls._entries.values())
            cls._entries.clear()

        for supervisor in entries:
            supervisor.stop()
