"""Процесс-зигота: прогретые импорты, fork на вызов, изоляция ребёнка.

Живёт внутри песочницы (bwrap с CAP_SYS_ADMIN в своём userns) и слушает
унаследованный socketpair. На запрос форкает ребёнка: тот уходит в свои
pid/mount/ipc/uts namespace, получает приватные /proc и /tmp, входит в
cgroup-leaf вызова (его вписывает хост по host-pid из SCM_CREDENTIALS),
ставит rlimits, сбрасывает capabilities и исполняет тело инструмента тем же
ToolMain-контрактом, что и холодный запуск.

Ошибки:
ZygoteProtocolError — сообщение не по контракту, работать дальше нельзя.
OSError — syscall изоляции отказал; хост видит это кодом выхода ребёнка.
"""

from __future__ import annotations

import array
import asyncio
import ctypes
import errno
import importlib
import inspect
import json
import logging
import os
import resource
import select
import signal
import socket
import sys
from collections.abc import Callable, Sequence
from enum import IntEnum, StrEnum
from types import ModuleType
from typing import Any, ClassVar, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import ToolLike, ToolMain
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.timing import Elapsed, ProcessAge

__all__ = [
    "CallExit",
    "CallRequest",
    "ChildLimits",
    "Isolation",
    "ZygoteEnv",
    "ZygoteMain",
    "ZygoteProtocolError",
    "ZygoteWire",
]

logger = logging.getLogger(__name__)


class ZygoteProtocolError(Exception):
    """Сообщение через сокет зиготы не соответствует контракту."""


class ZygoteEnv(StrEnum):
    """Переменные окружения процесса зиготы; их выставляет хост при spawn."""

    SOCKET_FD = "BOBA_ZYGOTE_FD"


class CloneFlag(IntEnum):
    """Флаги unshare(2), которые применяет ребёнок."""

    NEWNS = 0x00020000
    NEWUTS = 0x04000000
    NEWIPC = 0x08000000
    NEWPID = 0x20000000


class MountFlag(IntEnum):
    """Флаги mount(2) для приватного /tmp и ремоунта дерева."""

    NOSUID = 0x2
    NODEV = 0x4
    REC = 0x4000
    PRIVATE = 0x40000


class CallFd(IntEnum):
    """Порядок дескрипторов в SCM_RIGHTS запроса вызова."""

    STDIN = 0
    STDOUT = 1
    STDERR = 2
    RESULT = 3
    CONTROL = 4

    @classmethod
    def count(cls) -> int:
        return len(cls)


class ControlMark(StrEnum):
    """Байтовые метки пер-вызовного control-сокета."""

    BORN = "born"
    GO = "go"

    def bytes(self) -> bytes:
        return self.value.encode("ascii")


class ChildLimits(BaseModel):
    """Rlimits вызова; 0 — не выставлять. Едут в запросе, применяет ребёнок."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_memory_bytes: int = 0
    max_cpu_sec: int = 0
    max_file_size_bytes: int = 0
    max_open_files: int = 0
    oom_score_adj: int = 0

    def apply(self) -> None:
        if self.max_memory_bytes:
            pair = (self.max_memory_bytes, self.max_memory_bytes)
            resource.setrlimit(resource.RLIMIT_AS, pair)

        if self.max_cpu_sec:
            pair = (self.max_cpu_sec, self.max_cpu_sec)
            resource.setrlimit(resource.RLIMIT_CPU, pair)

        if self.max_file_size_bytes:
            pair = (self.max_file_size_bytes, self.max_file_size_bytes)
            resource.setrlimit(resource.RLIMIT_FSIZE, pair)

        if self.max_open_files:
            pair = (self.max_open_files, self.max_open_files)
            resource.setrlimit(resource.RLIMIT_NOFILE, pair)

        if self.oom_score_adj:
            with open("/proc/self/oom_score_adj", "w") as f:
                f.write(str(self.oom_score_adj))


class CallRequest(BaseModel):
    """Запрос вызова: argv тела, лимиты и режим изоляции."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "call"
    call_id: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    limits: ChildLimits
    isolate: bool
    tmp_bytes: int = Field(gt=0)


class CallExit(BaseModel):
    """Итог вызова: код выхода исполнителя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "exit"
    call_id: str = Field(min_length=1)
    code: int


class WarmupMessage(BaseModel):
    """Первое сообщение хоста: конфиги WARMUP-хуков по именам модулей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "warmup"
    configs: dict[str, dict[str, Any]]


class ZygoteReady(BaseModel):
    """Зигота прогрелась и принимает вызовы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "ready"
    warmup_ms: int


class ZygoteWire:
    """Кодек сообщений по SEQPACKET-сокету: JSON-датаграмма плюс SCM_RIGHTS."""

    MAX_MESSAGE: ClassVar[int] = 262_144
    MAX_FDS: ClassVar[int] = 16

    @staticmethod
    def send(sock: socket.socket, payload: BaseModel, fds: Sequence[int] = ()) -> None:
        data = payload.model_dump_json().encode("utf-8")

        ancillary = []
        if fds:
            packed = array.array("i", list(fds))
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, packed.tobytes())]

        sock.sendmsg([data], ancillary)

    @classmethod
    def recv(cls, sock: socket.socket) -> tuple[dict[str, object], list[int]]:
        """Сообщение и приехавшие дескрипторы; пустое сообщение — конец связи."""
        space = socket.CMSG_SPACE(cls.MAX_FDS * array.array("i").itemsize)
        data, ancdata, _flags, _addr = sock.recvmsg(cls.MAX_MESSAGE, space)

        fds: list[int] = []
        for level, kind, blob in ancdata:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                received = array.array("i")
                received.frombytes(blob[: len(blob) - len(blob) % received.itemsize])
                fds.extend(received)

        if not data:
            return {}, fds

        try:
            message = json.loads(data)
        except ValueError as exc:
            for fd in fds:
                os.close(fd)
            msg = f"zygote wire: not a JSON message: {data[:120]!r}"
            raise ZygoteProtocolError(msg) from exc

        if not isinstance(message, dict):
            for fd in fds:
                os.close(fd)
            msg = f"zygote wire: message must be an object, got {type(message)}"
            raise ZygoteProtocolError(msg)

        return message, fds


class Isolation:
    """Изоляция ребёнка своими syscall'ами: то, что bwrap делал бы на вызов."""

    _libc: ClassVar[ctypes.CDLL | None] = None

    @classmethod
    def libc(cls) -> ctypes.CDLL:
        if cls._libc is None:
            cls._libc = ctypes.CDLL(None, use_errno=True)

        return cls._libc

    @classmethod
    def unshare(cls, flags: int) -> None:
        if cls.libc().unshare(flags) != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"unshare({flags:#x}): {os.strerror(code)}")

    @classmethod
    def mount(
        cls, source: str, target: str, fstype: str, flags: int, data: str
    ) -> None:
        rc = cls.libc().mount(
            source.encode(), target.encode(), fstype.encode(), flags, data.encode()
        )
        if rc != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"mount({target}): {os.strerror(code)}")

    class _CapHeader(ctypes.Structure):
        _fields_: ClassVar = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class _CapData(ctypes.Structure):
        _fields_: ClassVar = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    _CAP_VERSION_3: ClassVar[int] = 0x20080522

    @classmethod
    def drop_capabilities(cls) -> None:
        header = cls._CapHeader(cls._CAP_VERSION_3, 0)
        data = (cls._CapData * 2)()
        if cls.libc().capset(ctypes.byref(header), ctypes.byref(data)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"capset: {os.strerror(code)}")

    @classmethod
    def enter_call_namespaces(cls, tmp_bytes: int) -> None:
        """Свои mount/ipc/uts ns, приватные /proc и /tmp; NEWPID ставит вызывающий."""
        cls.mount("none", "/", "", MountFlag.REC | MountFlag.PRIVATE, "")
        cls.mount("proc", "/proc", "proc", MountFlag.NOSUID | MountFlag.NODEV, "")
        cls.mount(
            "tmpfs",
            "/tmp",  # noqa: S108
            "tmpfs",
            MountFlag.NOSUID | MountFlag.NODEV,
            f"size={tmp_bytes}",
        )


class ZygoteMain:
    """Главный цикл зиготы: прогрев, приём запросов, fork и учёт детей."""

    REAP_POLL_SEC: ClassVar[float] = 0.1

    USERNS_SYSCTL: ClassVar[str] = "/proc/sys/user/max_user_namespaces"
    """Вложенные userns запрещаются до первого вызова, как в цепочке лаунчера."""

    def __init__(self, sock: socket.socket, tools: Sequence[ToolLike]) -> None:
        self._sock = sock
        self._tools = tools
        self._children: dict[int, tuple[str, socket.socket]] = {}

        # смерть ребёнка будит select немедленно: exit-репорт без опроса
        self._sigchld_r, self._sigchld_w = os.pipe()
        os.set_blocking(self._sigchld_r, False)
        os.set_blocking(self._sigchld_w, False)
        signal.signal(signal.SIGCHLD, self._wake_on_child)

    def _wake_on_child(self, _signum: int, _frame: object) -> None:
        try:
            os.write(self._sigchld_w, b"x")
        except BlockingIOError:
            return

    @classmethod
    def run(cls, module_names: Sequence[str]) -> int:
        """Вход процесса: прогрев модулей, ready, цикл обслуживания."""
        PayloadLogging.setup()

        raw_fd = os.environ.get(ZygoteEnv.SOCKET_FD)
        if raw_fd is None:
            print(f"{ZygoteEnv.SOCKET_FD} is not set", file=sys.stderr)  # noqa: T201
            return 2

        sock = socket.socket(fileno=int(raw_fd))

        warmup = Elapsed()
        tools: list[ToolLike] = []
        modules: dict[str, ModuleType] = {}
        for name in module_names:
            module = importlib.import_module(name)
            modules[name] = module
            tools.extend(module.TOOLS)

        cls._run_warmups(sock, modules)

        logger.info(
            "zygote up %dms after exec, warmed %d module(s) with %d tool(s) in %dms",
            ProcessAge.ms(),
            len(module_names),
            len(tools),
            warmup.ms(),
        )

        cls._block_new_userns()

        main = cls(sock, tools)
        ZygoteWire.send(sock, ZygoteReady(warmup_ms=warmup.ms()))

        return main.serve()

    WARMUP_ATTRIBUTE: ClassVar[str] = "WARMUP"

    @classmethod
    def _run_warmups(cls, sock: socket.socket, modules: dict[str, ModuleType]) -> None:
        """Первое сообщение хоста — конфиги WARMUP; хук исполняется до ready."""
        message, _fds = ZygoteWire.recv(sock)
        warmup = WarmupMessage.model_validate(message)

        for name, module in modules.items():
            hook = getattr(module, cls.WARMUP_ATTRIBUTE, None)
            if hook is None:
                continue

            raw = warmup.configs.get(name)
            if raw is None:
                logger.warning(
                    "zygote: module %s declares WARMUP but no config arrived, "
                    "skipping the hook",
                    name,
                )
                continue

            elapsed = Elapsed()
            asyncio.run(hook(cls._warmup_config(name, hook, raw)))
            logger.info("zygote: %s warmed up in %dms", name, elapsed.ms())

    @staticmethod
    def _warmup_config(
        name: str, hook: Callable[..., object], raw: dict[str, Any]
    ) -> object:
        """Конфиг хука: модель из аннотации его единственного параметра."""
        parameters = list(inspect.signature(hook).parameters)
        if len(parameters) != 1:
            msg = f"module {name}: WARMUP must take exactly one config parameter"
            raise ZygoteProtocolError(msg)

        hints = get_type_hints(hook)
        annotation = hints.get(parameters[0])
        if annotation is None:
            msg = f"module {name}: WARMUP parameter has no annotation"
            raise ZygoteProtocolError(msg)

        return TypeAdapter(annotation).validate_python(raw)

    @classmethod
    def _block_new_userns(cls) -> None:
        """Запись требует CAP_SYS_RESOURCE: есть только в песочном запуске.

        Голый запуск (тесты протокола) прав не имеет — там и изоляция
        выключена, поэтому отказ записи не скрывает угрозу, а отражает режим.
        """
        try:
            with open(cls.USERNS_SYSCTL, "w") as f:
                f.write("0")
        except PermissionError:
            logger.warning("zygote: %s is not writable, plain run", cls.USERNS_SYSCTL)
            return

        logger.info("zygote: %s=0, nested user namespaces denied", cls.USERNS_SYSCTL)

    def serve(self) -> int:
        while True:
            ready, _, _ = select.select(
                [self._sock, self._sigchld_r], [], [], self.REAP_POLL_SEC
            )

            if self._sigchld_r in ready:
                self._drain_wakeups()

            self._reap()

            if self._sock not in ready:
                continue

            message, fds = ZygoteWire.recv(self._sock)
            if not message and not fds:
                logger.info("zygote: host closed the socket, exiting")
                self._shutdown()
                return 0

            self._dispatch(message, fds)

    def _drain_wakeups(self) -> None:
        while True:
            try:
                if not os.read(self._sigchld_r, 256):
                    return
            except BlockingIOError:
                return

    def _dispatch(self, message: dict[str, object], fds: list[int]) -> None:
        try:
            request = CallRequest.model_validate(message)
        except ValueError:
            for fd in fds:
                os.close(fd)
            raise

        if len(fds) != CallFd.count():
            for fd in fds:
                os.close(fd)
            msg = (
                f"call {request.call_id}: expected {CallFd.count()} fds, "
                f"got {len(fds)}"
            )
            raise ZygoteProtocolError(msg)

        control = socket.socket(fileno=fds[CallFd.CONTROL])

        pid = os.fork()
        if pid == 0:
            self._child(request, fds)
            os._exit(127)

        for index in (CallFd.STDIN, CallFd.STDOUT, CallFd.STDERR, CallFd.RESULT):
            os.close(fds[index])

        self._children[pid] = (request.call_id, control)
        logger.info("zygote: call %s forked as pid %d", request.call_id, pid)

    def _reap(self) -> None:
        while self._children:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return

            if pid == 0:
                return

            entry = self._children.pop(pid, None)
            if entry is None:
                continue

            call_id, control = entry
            code = os.waitstatus_to_exitcode(status)
            logger.info("zygote: call %s finished rc=%d", call_id, code)

            try:
                ZygoteWire.send(control, CallExit(call_id=call_id, code=code))
            except OSError:
                logger.warning("zygote: call %s exit not delivered", call_id)
            finally:
                control.close()

    def _shutdown(self) -> None:
        for pid in self._children:
            with_signal = signal.SIGKILL
            try:
                os.kill(pid, with_signal)
            except ProcessLookupError:
                continue

        self._reap()

    def _child(self, request: CallRequest, fds: list[int]) -> None:
        """Первый форк: изоляция namespace'ов и второй форк под NEWPID."""
        try:
            self._sock.close()
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            os.close(self._sigchld_r)
            os.close(self._sigchld_w)

            if request.isolate:
                Isolation.unshare(
                    CloneFlag.NEWNS
                    | CloneFlag.NEWPID
                    | CloneFlag.NEWIPC
                    | CloneFlag.NEWUTS
                )

            pid = os.fork()
            if pid != 0:
                _, status = os.waitpid(pid, 0)
                os._exit(os.waitstatus_to_exitcode(status) & 0xFF)

            self._grandchild(request, fds)
        except BaseException as exc:
            print(f"zygote child failed: {exc}", file=sys.stderr)  # noqa: T201
            os._exit(126)

    def _grandchild(self, request: CallRequest, fds: list[int]) -> None:
        """Исполнитель: приватные /proc и /tmp, cgroup через хост, тело."""
        if request.isolate:
            Isolation.enter_call_namespaces(request.tmp_bytes)

        os.dup2(fds[CallFd.STDIN], 0)
        os.dup2(fds[CallFd.STDOUT], 1)
        os.dup2(fds[CallFd.STDERR], 2)
        os.environ[ToolChannel.RESULT.env_name] = str(fds[CallFd.RESULT])

        control = socket.socket(fileno=fds[CallFd.CONTROL])
        self._handshake(control)
        control.close()

        request.limits.apply()

        if request.isolate:
            Isolation.drop_capabilities()

        code = ToolMain.run(self._tools, list(request.argv))
        os._exit(code)

    @staticmethod
    def _handshake(control: socket.socket) -> None:
        """Хост узнаёт host-pid исполнителя и вписывает его в cgroup-leaf."""
        creds = array.array("i", [os.getpid(), os.getuid(), os.getgid()])
        control.sendmsg(
            [ControlMark.BORN.bytes()],
            [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, creds.tobytes())],
        )

        answer = control.recv(16)
        if answer != ControlMark.GO.bytes():
            msg = f"zygote handshake: expected go, got {answer!r}"
            raise ZygoteProtocolError(msg)


def main(argv: Sequence[str]) -> int:
    if not argv:
        print("usage: python -m boba.toolkit.zygote <module> [...]", file=sys.stderr)  # noqa: T201
        return errno.EINVAL

    return ZygoteMain.run(list(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
