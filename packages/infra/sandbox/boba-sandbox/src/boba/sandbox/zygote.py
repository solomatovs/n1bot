"""Хост-сторона зиготы: супервизор, спавнер из профиля и мост ToolLauncher.

Супервизор держит процесс-зиготу живым: поднимает при старте, перезапускает
после внезапной смерти, останавливается после исчерпания попыток старта.
Вызов при неготовой зиготе — ошибка инструмента, а не тихая деградация.

Ошибки:
ZygoteStartError — зигота не поднялась за отведённые попытки.
ZygoteUnavailableError — вызов пришёл, когда зигота не готова.
ZygoteCallError — зигота умерла посреди вызова, нарушила протокол либо
    вызов не отдал конверт tool_result.
Все три — LauncherError: наружу слой ничего сверх контракта порта не выпускает.
"""

from __future__ import annotations

import array
import logging
import os
import select
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import current_cancellation
from boba.sandbox.argv import build_zygote_argv
from boba.sandbox.cgroup import CgroupManager, GroupLimits
from boba.sandbox.profile import SandboxProfile
from boba.toolkit.channels import ToolChannel
from boba.toolkit.launcher import (
    LauncherError,
    LaunchOutcome,
    RunResult,
    ToolOutcome,
)
from boba.toolkit.protocol import REPLY, ToolCommand
from boba.toolkit.stream import ChannelSinks, ToolChannelsTap
from boba.toolkit.zygote import (
    CallExit,
    CallFd,
    CallRequest,
    ChildLimits,
    ControlMark,
    ZygoteEnv,
    ZygoteWire,
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
    """Политика жизненного цикла: таймауты старта и лимит попыток."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_timeout_sec: float = Field(gt=0)
    max_start_attempts: int = Field(ge=1)
    restart_backoff_sec: float = Field(ge=0)
    healthy_after_sec: float = Field(ge=0)
    """Прожила меньше — смерть считается неудачной попыткой старта."""


class ZygoteOutcome(BaseModel):
    """Итог вызова через зиготу."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    duration_ms: int
    timed_out: bool
    child_pid: int
    """Host-pid исполнителя; 0 — исполнитель не родился."""


class _CallChannels:
    """Дескрипторы одного вызова: пайпы каналов, stdin и control-пара."""

    def __init__(self) -> None:
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
        """В порядке CallFd: так их ждёт зигота."""
        table = {
            CallFd.STDIN: self.stdin_r,
            CallFd.STDOUT: self.stdout_w,
            CallFd.STDERR: self.stderr_w,
            CallFd.RESULT: self.result_w,
            CallFd.CONTROL: self.control_child.fileno(),
        }
        return [table[index] for index in CallFd]

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

    WAIT_STOP_SEC: ClassVar[float] = 5.0
    READ_CHUNK: ClassVar[int] = 65536

    def __init__(self, name: str, spawner: Spawner, policy: ZygotePolicy) -> None:
        self._name = name
        self._spawner = spawner
        self._policy = policy

        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._state = ZygoteState.STOPPED
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._attempts = 0
        self._monitor: threading.Thread | None = None

    @property
    def state(self) -> ZygoteState:
        return self._state

    def start(self) -> None:
        """Первый подъём; неудача всех попыток — ZygoteStartError."""
        with self._lock:
            if self._state in (ZygoteState.READY, ZygoteState.STARTING):
                return

            self._state = ZygoteState.STARTING
            self._attempts = 0

        while True:
            if self._try_start():
                self._watch()
                return

            with self._lock:
                self._attempts += 1
                if self._attempts >= self._policy.max_start_attempts:
                    self._state = ZygoteState.FAILED
                    msg = (
                        f"zygote {self._name}: not ready after "
                        f"{self._attempts} attempt(s)"
                    )
                    raise ZygoteStartError(msg)

            time.sleep(self._policy.restart_backoff_sec)

    def stop(self) -> None:
        with self._lock:
            self._state = ZygoteState.STOPPED
            proc = self._proc
            sock = self._sock
            self._proc = None
            self._sock = None

        if sock is not None:
            sock.close()

        if proc is None:
            return

        try:
            proc.wait(timeout=self.WAIT_STOP_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def call(  # noqa: PLR0913
        self,
        call_id: str,
        argv: Sequence[str],
        stdin_data: bytes,
        limits: ChildLimits,
        sinks: Mapping[ToolChannel, Callable[[bytes], None]],
        *,
        isolate: bool,
        tmp_bytes: int,
        timeout_sec: float,
        cgroup_procs: str = "",
    ) -> ZygoteOutcome:
        """Один вызов: запрос, handshake, насос каналов, код выхода."""
        with self._lock:
            sock = self._sock
            if self._state is not ZygoteState.READY or sock is None:
                msg = f"zygote {self._name} is not ready: {self._state}"
                raise ZygoteUnavailableError(msg)

        channels = _CallChannels()
        request = CallRequest(
            call_id=call_id,
            argv=tuple(argv),
            limits=limits,
            isolate=isolate,
            tmp_bytes=tmp_bytes,
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

        pump = _CallPump(
            name=self._name,
            request=request,
            channels=channels,
            sinks=sinks,
            timeout_sec=timeout_sec,
            cgroup_procs=cgroup_procs,
        )

        try:
            return pump.run(stdin_data)
        finally:
            channels.close_host_ends()

    def _try_start(self) -> bool:
        host_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)

        try:
            os.set_inheritable(child_sock.fileno(), True)
            proc = self._spawner(child_sock.fileno())
        except OSError as exc:
            host_sock.close()
            child_sock.close()
            logger.warning("zygote %s: spawn failed: %s", self._name, exc)
            return False

        child_sock.close()

        host_sock.settimeout(self._policy.start_timeout_sec)
        try:
            message, _fds = ZygoteWire.recv(host_sock)
        except (TimeoutError, OSError) as exc:
            logger.warning("zygote %s: no ready: %s", self._name, exc)
            self._abandon(proc, host_sock)
            return False

        if message.get("op") != "ready":
            logger.warning("zygote %s: unexpected hello: %s", self._name, message)
            self._abandon(proc, host_sock)
            return False

        host_sock.settimeout(None)

        with self._lock:
            self._proc = proc
            self._sock = host_sock
            self._state = ZygoteState.READY

        logger.info(
            "zygote %s: ready, pid=%d warmup=%sms",
            self._name,
            proc.pid,
            message.get("warmup_ms"),
        )
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

            logger.warning(
                "zygote %s: died unexpectedly rc=%s, restarting",
                self._name,
                watched.returncode,
            )

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

            if self._try_start():
                self._watch()
                return

            with self._lock:
                self._attempts += 1
                if self._attempts >= self._policy.max_start_attempts:
                    self._state = ZygoteState.FAILED
                    logger.error(
                        "zygote %s: restart attempts exhausted (%d), giving up",
                        self._name,
                        self._attempts,
                    )
                    return


class _CallPump:
    """Насос одного вызова: stdin, каналы, control-события, дедлайн, отмена."""

    READ_CHUNK: ClassVar[int] = 65536
    POLL_SEC: ClassVar[float] = 0.2

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        request: CallRequest,
        channels: _CallChannels,
        sinks: Mapping[ToolChannel, Callable[[bytes], None]],
        timeout_sec: float,
        cgroup_procs: str,
    ) -> None:
        self._name = name
        self._request = request
        self._channels = channels
        self._cgroup_procs = cgroup_procs

        self._started = time.monotonic()
        self._deadline = self._started + timeout_sec

        self._slots: dict[int, Callable[[bytes], None]] = {
            channels.stdout_r: sinks.get(ToolChannel.STDOUT, _discard),
            channels.stderr_r: sinks.get(ToolChannel.STDERR, _discard),
            channels.result_r: sinks.get(ToolChannel.RESULT, _discard),
        }
        self._open_reads = set(self._slots)

        self._child_pid = 0
        self._exit_code: int | None = None
        self._timed_out = False

    def run(self, stdin_data: bytes) -> ZygoteOutcome:
        pending = memoryview(stdin_data)
        os.set_blocking(self._channels.stdin_w, False)

        cancellation = current_cancellation()

        with cancellation.abort_with(self._kill):
            while self._exit_code is None or self._open_reads:
                if cancellation.cancelled:
                    self._kill()

                if self._deadline_hit():
                    break

                pending = self._step(pending)

        cancellation.raise_if_cancelled()

        return self._outcome()

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

        ready_r, ready_w, _ = select.select(rlist, wlist, [], self.POLL_SEC)

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
            written = os.write(self._channels.stdin_w, pending[: self.READ_CHUNK])
        except BrokenPipeError:
            self._channels.close_stdin()
            return memoryview(b"")

        rest = pending[written:]
        if rest.nbytes:
            return rest

        self._channels.close_stdin()
        return memoryview(b"")

    def _read(self, fd: int) -> None:
        chunk = os.read(fd, self.READ_CHUNK)
        if not chunk:
            self._open_reads.discard(fd)
            return

        self._slots[fd](chunk)

    def _control_event(self) -> None:
        """born с host-pid исполнителя либо exit с кодом; EOF — смерть зиготы."""
        space = socket.CMSG_SPACE(3 * array.array("i").itemsize)
        data, ancdata, _flags, _addr = self._channels.control_host.recvmsg(1024, space)

        if not data:
            msg = f"zygote {self._name}: control closed on call {self._request.call_id}"
            raise ZygoteCallError(msg)

        if data == ControlMark.BORN.bytes():
            self._child_pid = self._creds_pid(ancdata)
            self._enter_cgroup()
            self._channels.control_host.send(ControlMark.GO.bytes())
            return

        exit_message = CallExit.model_validate_json(data)
        self._exit_code = exit_message.code

    def _enter_cgroup(self) -> None:
        if not self._cgroup_procs:
            return

        with open(self._cgroup_procs, "w") as procs:
            procs.write(str(self._child_pid))

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
        )


def _discard(_data: bytes) -> None:
    """Приёмник канала без потребителя."""


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class ZygoteSpawner:
    """Запуск процесса зиготы из профиля песочницы.

    Требования к профилю проверяются при сборке: корень — премонтированный
    каталог (образ на вызов зиготе монтировать нечем и незачем), tmpfs на
    /tmp обязателен — из него дети получают приватный размерный /tmp.
    """

    TMP_PATH: ClassVar[str] = "/tmp"  # noqa: S108
    PYTHON: ClassVar[str] = "python3"
    MODULE: ClassVar[str] = "boba.toolkit.zygote"

    def __init__(self, profile: SandboxProfile, modules: Sequence[str]) -> None:
        try:
            profile = profile.render({})
        except RuntimeError as exc:
            msg = f"zygote profile has per-call path variables: {exc}"
            raise ZygoteStartError(msg) from exc

        if profile.rootfs_image:
            msg = (
                f"zygote profile carries rootfs_image {profile.rootfs_image!r}: "
                f"a premounted rootfs directory is required"
            )
            raise ZygoteStartError(msg)

        if not modules:
            msg = "zygote spawner: no tool modules to warm"
            raise ZygoteStartError(msg)

        self._tmp_bytes = self._tmp_size(profile)
        self._profile = profile
        self._modules = tuple(modules)

    @property
    def tmp_bytes(self) -> int:
        return self._tmp_bytes

    def spawn(self, fd: int) -> subprocess.Popen[bytes]:
        env = dict(self._profile.env_set)
        env[ZygoteEnv.SOCKET_FD.value] = str(fd)
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

        command = [self.PYTHON, "-m", self.MODULE, *self._modules]
        argv = build_zygote_argv(self._profile, command, env=env)

        return subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            pass_fds=(fd,),
            env=dict(os.environ),
        )

    @classmethod
    def _tmp_size(cls, profile: SandboxProfile) -> int:
        for spec in profile.tmpfs:
            if spec.path == cls.TMP_PATH:
                return spec.size_bytes

        msg = "zygote profile must mount tmpfs on /tmp: children clone its size"
        raise ZygoteStartError(msg)


class _EnvelopeSink:
    """Конверт tool_result вызова: маленький канал копится целиком."""

    def __init__(self) -> None:
        self._data = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._data.extend(chunk)

    def data(self) -> bytes:
        return bytes(self._data)


class _TailSink:
    """Хвост канала: объяснение сбоя, когда конверта нет."""

    LIMIT: ClassVar[int] = 4096

    def __init__(self) -> None:
        self._tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._tail.extend(chunk)
        if len(self._tail) > self.LIMIT:
            del self._tail[: len(self._tail) - self.LIMIT]

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")


class ZygoteToolCaller:
    """Реализация ToolLauncher поверх зиготы: команда модуля -> конверт.

    call_text зиготой не обслуживается до этапа bash: текстовые команды идут
    холодным путём.
    """

    ARGV_HEAD: ClassVar[int] = 3
    """python3 -m <module> — префикс команды модуля инструментов."""

    def __init__(
        self,
        tool: str,
        supervisor: ZygoteSupervisor,
        profile: SandboxProfile,
    ) -> None:
        self._tool = tool
        self._supervisor = supervisor
        self._profile = profile
        self._tmp_bytes = ZygoteSpawner._tmp_size(profile)

    @property
    def supervisor(self) -> ZygoteSupervisor:
        return self._supervisor

    def call_text(self, command: str, stdin: str) -> LaunchOutcome:
        msg = f"zygote {self._tool}: call_text is not served by the zygote"
        raise ZygoteCallError(msg)

    def run_tool(self, command: ToolCommand) -> ToolOutcome:
        """Граница слоя: наружу только ошибки из контракта модуля."""
        argv_tail = self._argv_tail(command)

        envelope = _EnvelopeSink()
        stderr_tail = _TailSink()

        sinks: dict[ToolChannel, Callable[[bytes], None]] = {
            ToolChannel.RESULT: envelope.feed,
            ToolChannel.STDERR: stderr_tail.feed,
        }

        if journal := ToolChannelsTap.get():
            sinks = self._teed(sinks, journal)

        outcome = self._grouped_call(command, argv_tail, sinks)

        reply_raw = envelope.data()
        if not reply_raw:
            msg = (
                f"{self._tool}: no envelope on tool_result "
                f"(rc={outcome.exit_code}, timed_out={outcome.timed_out}); "
                f"tool_stderr={stderr_tail.text()!r}"
            )
            raise ZygoteCallError(msg)

        try:
            reply = REPLY.validate_json(reply_raw)
        except ValueError as exc:
            msg = f"{self._tool}: envelope does not match contract: {exc}"
            raise ZygoteCallError(msg) from exc

        run = RunResult(
            exit_code=outcome.exit_code,
            stdout="",
            stderr=stderr_tail.text(),
            duration_ms=outcome.duration_ms,
            timed_out=outcome.timed_out,
        )
        return ToolOutcome(reply=reply, run=run, diagnostic="")

    def _grouped_call(
        self,
        command: ToolCommand,
        argv_tail: tuple[str, ...],
        sinks: Mapping[ToolChannel, Callable[[bytes], None]],
    ) -> ZygoteOutcome:
        """Вызов в собственном cgroup-leaf'е, если профиль его требует."""
        group = GroupLimits.of_profile(self._profile)

        if not group.requested:
            return self._call(command, argv_tail, sinks, cgroup_procs="")

        manager = CgroupManager(self._profile.cgroup_base)
        leaf = manager.acquire(group)

        try:
            procs = os.path.join(leaf, "cgroup.procs")
            return self._call(command, argv_tail, sinks, cgroup_procs=procs)
        finally:
            if note := manager.throttling(leaf):
                logger.warning("zygote[%s]: %s", self._tool, note)
            manager.release(leaf)

    def _call(
        self,
        command: ToolCommand,
        argv_tail: tuple[str, ...],
        sinks: Mapping[ToolChannel, Callable[[bytes], None]],
        *,
        cgroup_procs: str,
    ) -> ZygoteOutcome:
        timeout_sec = self._profile.timeout_sec
        if timeout_sec is None:
            msg = f"zygote {self._tool}: profile without timeout_sec"
            raise ZygoteCallError(msg)

        limits = ChildLimits(
            max_memory_bytes=self._profile.max_memory_bytes,
            max_cpu_sec=self._profile.max_cpu_sec,
            max_file_size_bytes=self._profile.max_file_size_bytes,
            max_open_files=self._profile.max_open_files,
            oom_score_adj=self._profile.oom_score_adj,
        )

        return self._supervisor.call(
            uuid.uuid4().hex,
            argv_tail,
            command.stdin,
            limits,
            sinks,
            isolate=True,
            tmp_bytes=self._tmp_bytes,
            timeout_sec=float(timeout_sec),
            cgroup_procs=cgroup_procs,
        )

    def _argv_tail(self, command: ToolCommand) -> tuple[str, ...]:
        """Команда модуля без префикса python: имя тула и флаги."""
        argv = command.argv
        if len(argv) <= self.ARGV_HEAD or argv[1] != "-m":
            msg = f"{self._tool}: not a tool module command: {argv[:3]}"
            raise ZygoteCallError(msg)

        return argv[self.ARGV_HEAD :]

    @staticmethod
    def _teed(
        sinks: dict[ToolChannel, Callable[[bytes], None]],
        journal: ChannelSinks,
    ) -> dict[ToolChannel, Callable[[bytes], None]]:
        """Тройники: свои приёмники конверта и хвоста плюс журнал вызова."""
        teed: dict[ToolChannel, Callable[[bytes], None]] = {}
        for channel in (ToolChannel.STDOUT, ToolChannel.STDERR, ToolChannel.RESULT):
            own = sinks.get(channel)
            journal_sink = journal.sink_of(channel).feed
            if own is None:
                teed[channel] = journal_sink
                continue

            def both(
                chunk: bytes,
                first: Callable[[bytes], None] = own,
                second: Callable[[bytes], None] = journal_sink,
            ) -> None:
                first(chunk)
                second(chunk)

            teed[channel] = both

        return teed


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
    ) -> ZygoteSupervisor:
        """Живой супервизор секции; при отсутствии — поднять и запомнить."""
        with cls._lock:
            existing = cls._entries.get(name)
            if existing is not None and existing.state is not ZygoteState.STOPPED:
                return existing

            spawner = ZygoteSpawner(profile, modules)
            supervisor = ZygoteSupervisor(name, spawner.spawn, policy)
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
