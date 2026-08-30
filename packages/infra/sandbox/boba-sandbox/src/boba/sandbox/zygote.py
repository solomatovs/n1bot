"""Хост-сторона зиготы: супервизор, спавнер из профиля и мост ToolLauncher.

Супервизор держит процесс-зиготу живым: поднимает при старте, перезапускает
после внезапной смерти, останавливается после исчерпания попыток старта.
Вызов при неготовой зиготе — ошибка инструмента, а не тихая деградация.

Ошибки:
ZygoteStartError — зигота не поднялась за отведённые попытки.
ZygoteUnavailableError — вызов пришёл, когда зигота не готова.
ZygoteCallError — зигота умерла посреди вызова, нарушила протокол, не отдала
    конверт tool_result либо превысила потолок канала.
SandboxChainError — исполнитель сообщил, что корень секции не смонтирован.
SandboxMountError — исполнитель сообщил, что образ вызова не смонтирован.
Все пять — LauncherError: наружу слой ничего сверх контракта порта не выпускает.
"""

from __future__ import annotations

import array
import logging
import os
import select
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
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import current_cancellation
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
from boba.toolkit.channels import ToolChannel
from boba.toolkit.launcher import (
    LauncherError,
    LaunchOutcome,
    RunResult,
    ToolLauncher,
    ToolOutcome,
)
from boba.toolkit.protocol import REPLY, ToolCommand
from boba.toolkit.stream import (
    ChannelSinks,
    Chunk,
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
    """Секция [sandbox.zygote]: как супервизор поднимает и перезапускает зиготу.

    Зигота секции инструментов — резидентный процесс: приложение поднимает его
    при старте и держит живым, форкая на каждый вызов. Эти четыре значения
    описывают, сколько ждать готовности, сколько раз пробовать снова и когда
    считать зиготу здоровой.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_timeout_sec: float = Field(
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
        ge=0,
        description=(
            "Пауза между попытками старта. Зигота, падающая сразу после "
            "запуска, без паузы крутила бы перезапуск десятки раз в секунду и "
            "занимала бы процессор вместо того, чтобы дать администратору "
            "увидеть причину в логе."
        ),
    )
    stop_wait_sec: float = Field(
        gt=0,
        description=(
            "Сколько секунд ждать, пока зигота выйдет сама после закрытия "
            "сокета, прежде чем добить её сигналом. Штатный выход занимает "
            "миллисекунды; ожидание нужно на случай, когда зигота доживает "
            "последний вызов."
        ),
    )
    call_poll_sec: float = Field(
        gt=0,
        description=(
            "Шаг опроса дескрипторов вызова на стороне приложения: как часто "
            "насос просыпается, если ни один канал не отдал данных. Определяет "
            "точность срабатывания таймаута вызова."
        ),
    )
    healthy_after_sec: float = Field(
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
    """Итог вызова через зиготу."""

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
    """Дескрипторы одного вызова: пайпы каналов, stdin, control-пара и cgroup."""

    def __init__(self, cgroup_fd: int = -1) -> None:
        self.cgroup_fd = cgroup_fd
        self.stdin_r, self.stdin_w = os.pipe()
        self.stdout_r, self.stdout_w = os.pipe()
        self.stderr_r, self.stderr_w = os.pipe()
        self.result_r, self.result_w = os.pipe()
        self.control_host, self.control_child = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET
        )
        self.control_host.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self._stdin_open = True

    def child_fds(self) -> list[int]:
        """В порядке CallFd: так их ждёт зигота; cgroup — последним и не всегда."""
        listed = [
            self.stdin_r,
            self.stdout_w,
            self.stderr_w,
            self.result_w,
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
        self.control_child.close()

    def close_stdin(self) -> None:
        if not self._stdin_open:
            return

        self._stdin_open = False
        os.close(self.stdin_w)

    def close_host_ends(self) -> None:
        self.close_stdin()
        os.close(self.stdout_r)
        os.close(self.stderr_r)
        os.close(self.result_r)
        self.control_host.close()


class ZygoteSupervisor:
    """Держит зиготу живой и прогоняет вызовы через её socketpair."""

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
        self._stderr = _TailSink(stderr_tail_bytes)
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

    def call(  # noqa: PLR0913
        self,
        call_id: str,
        argv: Sequence[str],
        stdin_data: bytes,
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
        """Один вызов: запрос, handshake, насос каналов, код выхода."""
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
            msg = f"zygote {self._name}: request not sent: {exc}"
            raise ZygoteUnavailableError(msg) from exc

        channels.close_child_ends()
        if cgroup_fd >= 0:
            os.close(cgroup_fd)

        pump = _CallPump(
            name=self._name,
            request=request,
            channels=channels,
            sinks=sinks,
            timeout_sec=timeout_sec,
            poll_sec=self._policy.call_poll_sec,
            kill_grace_sec=kill_grace_sec,
        )

        try:
            return pump.run(stdin_data)
        finally:
            channels.close_host_ends()
            with self._lock:
                self._in_flight.pop(call_id, None)

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

        self._stderr = _TailSink(self._tail_bytes)
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
class _ReadSlot:
    """Читаемый канал вызова: читатель дескриптора и приёмник порций."""

    reader: FdReader
    sink: ChunkSink

    @classmethod
    def of(cls, fd: int, sink: ChunkSink) -> _ReadSlot:
        return cls(reader=FdReader(fd), sink=sink)


class _CallPump:
    """Насос одного вызова: stdin, каналы, control-события, дедлайн, отмена."""

    WRITE_CHUNK: ClassVar[int] = FdReader.CHUNK

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        request: CallRequest,
        channels: _CallChannels,
        sinks: Mapping[ToolChannel, ChunkSink],
        timeout_sec: float,
        poll_sec: float,
        kill_grace_sec: float,
    ) -> None:
        self._name = name
        self._request = request
        self._channels = channels
        self._poll_sec = poll_sec
        self._kill_grace_sec = kill_grace_sec

        self._started = time.monotonic()
        self._deadline = self._started + timeout_sec

        self._slots: dict[int, _ReadSlot] = {
            channels.stdout_r: _ReadSlot.of(
                channels.stdout_r, sinks.get(ToolChannel.STDOUT, _discard)
            ),
            channels.stderr_r: _ReadSlot.of(
                channels.stderr_r, sinks.get(ToolChannel.STDERR, _discard)
            ),
            channels.result_r: _ReadSlot.of(
                channels.result_r, sinks.get(ToolChannel.RESULT, _discard)
            ),
        }
        self._open_reads = set(self._slots)

        self._child_pid = 0
        self._exit_code: int | None = None
        self._timed_out = False
        self._setup_failure = SetupFailure.NONE
        self._setup_detail = ""

    def run(self, stdin_data: bytes) -> ZygoteOutcome:
        pending = memoryview(stdin_data)
        os.set_blocking(self._channels.stdin_w, False)

        cancellation = current_cancellation()

        try:
            with cancellation.abort_with(self._kill):
                while self._exit_code is None or self._open_reads:
                    if cancellation.cancelled:
                        self._kill()

                    if self._deadline_hit():
                        break

                    pending = self._step(pending)
        except BaseException:
            # сорвался приёмник вывода: без добивания исполнитель и его
            # fuse2fs остались бы жить и держать образ пользователя
            self._abort()
            raise

        cancellation.raise_if_cancelled()

        return self._outcome()

    def _abort(self) -> None:
        """Добить исполнителя и дождаться выхода: образ должен отпуститься."""
        self._kill()

        for fd, slot in list(self._slots.items()):
            self._slots[fd] = replace(slot, sink=_discard)

        deadline = time.monotonic() + self._kill_grace_sec
        pending = memoryview(b"")

        while self._exit_code is None and time.monotonic() < deadline:
            try:
                pending = self._step(pending)
            except (OSError, ZygoteCallError):
                return

    def _deadline_hit(self) -> bool:
        """True — дедлайн истёк и ждать больше некого."""
        if time.monotonic() < self._deadline:
            return False

        if not self._timed_out:
            self._timed_out = True
            self._kill()

        return self._child_pid == 0

    def _step(self, pending: memoryview) -> memoryview:
        rlist = list(self._open_reads)
        if self._exit_code is None:
            rlist.append(self._channels.control_host.fileno())

        wlist: list[int] = []
        if pending.nbytes:
            wlist.append(self._channels.stdin_w)

        ready_r, ready_w, _ = select.select(rlist, wlist, [], self._poll_sec)

        if ready_w:
            pending = self._feed(pending)

        for fd in ready_r:
            if fd == self._channels.control_host.fileno():
                self._control_event()
                continue

            self._read(fd)

        return pending

    def _feed(self, pending: memoryview) -> memoryview:
        try:
            written = os.write(self._channels.stdin_w, pending[: self.WRITE_CHUNK])
        except BrokenPipeError:
            self._channels.close_stdin()
            return memoryview(b"")

        rest = pending[written:]
        if rest.nbytes:
            return rest

        self._channels.close_stdin()
        return memoryview(b"")

    def _read(self, fd: int) -> None:
        slot = self._slots[fd]
        chunk = slot.reader.read()
        if not chunk:
            self._open_reads.discard(fd)
            return

        slot.sink(chunk)

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

    def _kill(self) -> None:
        if self._child_pid:
            _kill_quietly(self._child_pid)

    def _outcome(self) -> ZygoteOutcome:
        exit_code = self._exit_code

        if exit_code is None:
            if not self._timed_out:
                msg = f"zygote {self._name}: died mid-call {self._request.call_id}"
                raise ZygoteCallError(msg)

            exit_code = -int(signal.SIGKILL)

        return ZygoteOutcome(
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - self._started) * 1000),
            timed_out=self._timed_out,
            child_pid=self._child_pid,
            setup_failure=self._setup_failure,
            setup_detail=self._setup_detail,
        )


def _discard(_data: Chunk) -> None:
    """Приёмник канала без потребителя."""


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class ZygoteMounts:
    """Пути обвязки образа внутри зиготы: точки фиксированы SandboxLayout."""

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
    """Запуск процесса зиготы из профиля песочницы.

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


class _CappedBuffer:
    """Канал целиком, но не длиннее потолка: конверт и вывод shell-команды.

    Тело вызова живёт под лимитом памяти своей группы, а копится его вывод в
    памяти приложения, на которую эти лимиты не распространяются: без
    потолка тело выносит хост потоком в несколько гигабайт. Превышение
    обрывает вызов — насос добивает исполнителя, а причина уезжает в чат и
    модели обычной ошибкой инструмента.
    """

    def __init__(self, limit: int, channel: ToolChannel) -> None:
        self._limit = limit
        self._channel = channel
        self._data = bytearray()

    def feed(self, chunk: Chunk) -> None:
        self._data.extend(chunk)
        if len(self._data) <= self._limit:
            return

        msg = (
            f"sandbox: {self._channel.value} exceeded {self._limit} bytes; "
            "the call was killed"
        )
        raise ZygoteCallError(msg)

    def text(self) -> str:
        return self._data.decode("utf-8", errors="replace")

    def data(self) -> bytearray:
        """Конверт как есть: pydantic разбирает bytes-like без копии."""
        return self._data


class _Tee:
    """Тройник двух приёмников одного канала."""

    def __init__(self, first: ChunkSink, second: ChunkSink) -> None:
        self._first = first
        self._second = second

    def feed(self, chunk: Chunk) -> None:
        self._first(chunk)
        self._second(chunk)


class _RelayTee(StderrTee):
    """Tee релея: сырые строки stderr — в журнал вызова и в свой приёмник."""

    def __init__(
        self, sinks: ChannelSinks | None, own: _CappedBuffer | _TailSink
    ) -> None:
        super().__init__(sinks, ToolChannel.STDERR)
        self._own = own

    def raw(self, line: str) -> None:
        super().raw(line)
        self._own.feed(f"{line}\n".encode())


class _TailSink:
    """Хвост канала: объяснение сбоя, когда конверта нет."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._tail = bytearray()

    def feed(self, chunk: Chunk) -> None:
        self._tail.extend(chunk)
        if len(self._tail) > self._limit:
            del self._tail[: len(self._tail) - self._limit]

    def text(self) -> str:
        return self._tail.decode("utf-8", errors="replace")


class ZygoteToolCaller(ToolLauncher):
    """Реализация ToolLauncher поверх зиготы: команда модуля -> конверт."""

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

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        """Shell-команда в изолированном ребёнке: stdout/stderr/rc как есть."""
        limit = self._profile.host.channel_limit_bytes
        stdout = _CappedBuffer(limit, ToolChannel.STDOUT)
        stderr = _CappedBuffer(limit, ToolChannel.STDERR)

        relay = SandboxLogRelay(self._tool, _RelayTee(ToolChannelsTap.get(), stderr))
        sinks = self._sinks(ToolChannel.STDOUT, stdout.feed, relay)

        shell = self._profile.run.shell
        if not shell:
            msg = f"{self._tool}: profile declares no shell for text commands"
            raise ZygoteCallError(msg)

        argv = (shell, "-c", command)
        try:
            outcome = self._grouped_call(
                ToolCommand(argv=argv, stdin=stdin.encode("utf-8")),
                argv,
                sinks,
                kind=CallKind.SHELL,
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

    def run_tool(self, command: ToolCommand) -> ToolOutcome:
        """Граница слоя: наружу только ошибки из контракта модуля."""
        argv_tail = self._argv_tail(command)

        envelope = _CappedBuffer(
            self._profile.host.channel_limit_bytes, ToolChannel.RESULT
        )
        stderr_tail = _TailSink(self._profile.host.stderr_tail_bytes)

        relay = SandboxLogRelay(
            self._tool, _RelayTee(ToolChannelsTap.get(), stderr_tail)
        )
        sinks = self._sinks(ToolChannel.RESULT, envelope.feed, relay)

        try:
            outcome = self._grouped_call(command, argv_tail, sinks)
        finally:
            relay.flush()

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

        reply_raw = envelope.data()
        if not reply_raw:
            msg = (
                f"{self._tool}: no envelope on tool_result "
                f"(rc={outcome.exit_code}, timed_out={outcome.timed_out}); "
                f"tool_stderr={stderr_tail.text()!r}"
            )
            if diagnostic:
                msg = f"{msg}; {diagnostic}"

            raise ZygoteCallError(msg)

        try:
            reply = REPLY.validate_json(reply_raw)
        except ValueError as exc:
            msg = f"{self._tool}: envelope does not match contract: {exc}"
            raise ZygoteCallError(msg) from exc

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

    def _grouped_call(
        self,
        command: ToolCommand,
        argv_tail: tuple[str, ...],
        sinks: Mapping[ToolChannel, ChunkSink],
        *,
        kind: CallKind = CallKind.MODULE,
    ) -> ZygoteOutcome:
        """Вызов в собственном cgroup-leaf'е, если профиль его требует."""
        group = GroupLimits.of_profile(self._profile)

        if not group.requested:
            return self._call(command, argv_tail, sinks, cgroup_leaf="", kind=kind)

        manager = CgroupManager(self._profile.host.cgroup_base)
        leaf = manager.acquire(group)

        try:
            return self._call(
                command, argv_tail, sinks, cgroup_leaf=str(leaf), kind=kind
            )
        finally:
            if note := manager.throttling(leaf):
                logger.warning("zygote[%s]: %s", self._tool, note)
            manager.release(leaf)

    def _call(
        self,
        command: ToolCommand,
        argv_tail: tuple[str, ...],
        sinks: Mapping[ToolChannel, ChunkSink],
        *,
        cgroup_leaf: str,
        kind: CallKind = CallKind.MODULE,
    ) -> ZygoteOutcome:
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

        return self._supervisor.call(
            uuid.uuid4().hex,
            argv_tail,
            command.stdin,
            limits,
            sinks,
            isolate=True,
            mounts=self._call_mounts,
            timeout_sec=float(timeout_sec),
            kill_grace_sec=self._profile.host.kill_grace_sec,
            cgroup_leaf=cgroup_leaf,
            images=images,
            mounting=self._mounting_of(images),
            staging=ZygoteMounts(rendered).staging(),
            cwd=rendered.run.cwd,
            kind=kind,
        )

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

    @staticmethod
    def _sinks(
        own_channel: ToolChannel,
        own: ChunkSink,
        relay: SandboxLogRelay,
    ) -> dict[ToolChannel, ChunkSink]:
        """Приёмники вызова: stderr — через релей кадров, остальное — тройником.

        Кадры `sandbox-mount:`/`sandbox-log:` из ребёнка поднимаются в журнал
        приложения и не попадают ни в вывод команды, ни в хвост tool_stderr.
        """
        sinks: dict[ToolChannel, ChunkSink] = {
            ToolChannel.STDERR: relay.feed,
        }

        journal = ToolChannelsTap.get()
        journal_sink: ChunkSink | None = None
        if journal is not None:
            journal_sink = journal.sink_of(own_channel).feed

        if journal_sink is None:
            sinks[own_channel] = own
        else:
            sinks[own_channel] = _Tee(own, journal_sink).feed

        if journal is not None:
            for channel in (ToolChannel.STDOUT, ToolChannel.RESULT):
                if channel not in sinks:
                    sinks[channel] = journal.sink_of(channel).feed

        return sinks


class ZygoteRegistry:
    """Реестр супервизоров процесса: одна живая зигота на tool-секцию.

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
