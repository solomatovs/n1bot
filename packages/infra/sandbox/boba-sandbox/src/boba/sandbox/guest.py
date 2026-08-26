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

import argparse
import array
import asyncio
import ctypes
import errno
import importlib
import json
import logging
import os
import resource
import select
import signal
import socket
import sys
from collections.abc import Mapping, Sequence
from enum import IntEnum, StrEnum
from types import ModuleType
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import ToolLike, ToolMain
from boba.toolkit.facade import WarmupHooks
from boba.toolkit.payload import PayloadLogging
from boba.toolkit.timing import Elapsed, ProcessAge
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.images import (
    FuseMounter,
    ImageStore,
    LauncherOptions,
    MountError,
    SparseCopier,
    trace,
)

__all__ = [
    "CallExit",
    "CallMounts",
    "CallRequest",
    "ChildLimits",
    "Isolation",
    "ZygoteArgs",
    "ZygoteMain",
    "ZygoteProtocolError",
    "ZygoteWire",
]

logger = logging.getLogger(__name__)


class ZygoteProtocolError(Exception):
    """Сообщение через сокет зиготы не соответствует контракту."""


class ZygoteArgs(BaseModel):
    """Аргументы запуска зиготы: всё, что задаёт профиль, приезжает в argv.

    Ничего не передаётся окружением: параметры видны в командной строке
    процесса и разбираются один раз здесь.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SOCKET_FD: ClassVar[str] = "--socket-fd"
    REAP_POLL_SEC: ClassVar[str] = "--reap-poll-sec"
    LOG_LEVEL: ClassVar[str] = "--log-level"

    socket_fd: int = Field(ge=0)
    reap_poll_sec: float = Field(gt=0)
    log_level: str = Field(min_length=1)
    """Уровень логера приложения: зигота и тела инструментов пишут им же."""
    modules: tuple[str, ...] = ()

    @classmethod
    def parse(cls, argv: Sequence[str]) -> ZygoteArgs:
        parser = argparse.ArgumentParser(prog="boba-zygote")
        parser.add_argument(cls.SOCKET_FD, type=int, required=True)
        parser.add_argument(cls.REAP_POLL_SEC, type=float, required=True)
        parser.add_argument(cls.LOG_LEVEL, type=str, required=True)
        parser.add_argument("modules", nargs="*")

        parsed = parser.parse_args(list(argv))
        return cls(
            socket_fd=parsed.socket_fd,
            reap_poll_sec=parsed.reap_poll_sec,
            log_level=parsed.log_level,
            modules=tuple(parsed.modules),
        )

    def render(self) -> list[str]:
        """Аргументы командной строки в порядке разбора."""
        return [
            self.SOCKET_FD,
            str(self.socket_fd),
            self.REAP_POLL_SEC,
            str(self.reap_poll_sec),
            self.LOG_LEVEL,
            self.log_level,
            *self.modules,
        ]


class SetupTiming:
    """Тайминг подготовки вызова: по нему видно, какая фаза съела время.

    Кадр уезжает в журнал приложения тем же каналом, что и ход монтирования,
    поэтому медленный вызов разбирается без отдельной пробы.
    """

    def __init__(self) -> None:
        self._total = Elapsed()
        self._step = Elapsed()
        self._marks: list[str] = []

    def mark(self, name: str) -> None:
        self._marks.append(f"{name} {self._step.ms()}ms")
        self._step = Elapsed()

    def report(self, call_id: str, phase: str = "setup") -> None:
        listed = ", ".join(self._marks)
        trace(f"call {call_id} {phase} {self._total.ms()}ms: {listed}")


class CloneFlag(IntEnum):
    """Флаги unshare(2)/clone3(2), которые применяет ребёнок."""

    NEWNS = 0x00020000
    NEWUTS = 0x04000000
    NEWIPC = 0x08000000
    NEWPID = 0x20000000
    INTO_CGROUP = 0x200000000
    """clone3: ребёнок рождается сразу в переданном cgroup, без миграции."""


class MountFlag(IntEnum):
    """Флаги mount(2)/umount2(2) для приватных точек вызова."""

    NOSUID = 0x2
    NODEV = 0x4
    REC = 0x4000
    PRIVATE = 0x40000
    DETACH = 0x2
    """MNT_DETACH: точка отцепляется от дерева, открытые файлы живут дальше."""


class CallFd(IntEnum):
    """Порядок дескрипторов в SCM_RIGHTS запроса вызова."""

    STDIN = 0
    STDOUT = 1
    STDERR = 2
    RESULT = 3
    CONTROL = 4
    CGROUP = 5
    """Каталог cgroup-leaf'а; едет только когда у вызова есть групповые лимиты."""

    @classmethod
    def count(cls) -> int:
        """Обязательные дескрипторы: cgroup среди них нет."""
        return len(cls) - 1


class CallKind(StrEnum):
    """Что исполняет ребёнок: модуль инструментов либо shell-команду."""

    MODULE = "module"
    SHELL = "shell"


class ControlMark(StrEnum):
    """Байтовые метки пер-вызовного control-сокета."""

    BORN = "born"

    def bytes(self) -> bytes:
        return self.value.encode("ascii")


class ChildLimits(BaseModel):
    """Лимиты вызова; 0 — не выставлять. Едут в запросе, применяет ребёнок."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_memory_bytes: int = 0
    max_cpu_sec: int = 0
    max_file_size_bytes: int = 0
    max_open_files: int = 0
    oom_score_adj: int = 0
    cpu_cores: int = 0
    """Ядер по cgroup-квоте профиля: столько же ставится маской affinity."""

    def apply(self) -> None:
        if self.cpu_cores:
            self._pin_cpus()

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

    def _pin_cpus(self) -> None:
        """Маска ядер по квоте профиля.

        cgroup-квоту нативные движки не видят и размер пула берут из маски
        доступных ядер: без неё под квотой в одно ядро поднимается пул на всю
        машину. Окно ядер сдвигается по pid, чтобы параллельные вызовы не
        толпились на одних и тех же.
        """
        available = sorted(os.sched_getaffinity(0))
        if len(available) <= self.cpu_cores:
            return

        start = (os.getpid() * self.cpu_cores) % len(available)
        window = available[start : start + self.cpu_cores]

        if len(window) < self.cpu_cores:
            window += available[: self.cpu_cores - len(window)]

        os.sched_setaffinity(0, set(window))


class ImageMount(BaseModel):
    """rw-образ вызова: файл, точка монтирования и шаблон первой копии."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = Field(min_length=1)
    target: str = Field(min_length=1)
    template: str = Field(min_length=1)
    """Откуда копировать образ, если его ещё нет; путь внутри песочницы."""


class ImageMounting(BaseModel):
    """Как ребёнок монтирует образы: шаблон, тайминги, где лежит fuse2fs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fuse2fs_dir: str = Field(min_length=1)
    mount_wait_sec: float = Field(gt=0)
    mount_poll_sec: float = Field(gt=0)
    shutdown_wait_sec: float = Field(gt=0)
    lock_wait_sec: float = Field(gt=0)
    copy_chunk_bytes: int = Field(gt=0)

    def options(self) -> LauncherOptions:
        return LauncherOptions(
            mount_wait_sec=self.mount_wait_sec,
            mount_poll_sec=self.mount_poll_sec,
            shutdown_wait_sec=self.shutdown_wait_sec,
            lock_wait_sec=self.lock_wait_sec,
            copy_chunk_bytes=self.copy_chunk_bytes,
        )


class CallMounts(BaseModel):
    """Приватные точки вызова: что исполнитель перемонтирует себе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proc: str = ""
    """Точка procfs из профиля; пусто — профиль её не объявил."""
    tmp: str = ""
    """Приватная tmpfs вызова из профиля; пусто — общая с зиготой."""
    tmp_bytes: int = 0


class CallRequest(BaseModel):
    """Запрос вызова: argv тела, лимиты, изоляция, rw-образы и cwd."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "call"
    call_id: str = Field(min_length=1)
    kind: CallKind = CallKind.MODULE
    argv: tuple[str, ...] = Field(min_length=1)
    limits: ChildLimits
    isolate: bool
    mounts: CallMounts
    images: tuple[ImageMount, ...] = ()
    mounting: ImageMounting | None = None
    """Параметры монтирования; None — образов у вызова нет."""
    staging: tuple[str, ...] = ()
    """Обвязка монтирования: шаблон, fuse2fs и каталог образов пользователей.

    Исполнитель отцепляет её от себя всегда, а не только когда монтирует
    образ: секции без workspace иначе оставляли телу инструмента каталог с
    образами всех пользователей. Путь, пришедший частью большего
    монтирования (лежит в самом корне), не отцепляется — прятать там нечего.
    """
    cwd: str = ""
    into_cgroup: bool = False
    """Шестым дескриптором приехал каталог cgroup-leaf'а вызова."""


class CallExit(BaseModel):
    """Итог вызова: код выхода исполнителя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "exit"
    call_id: str = Field(min_length=1)
    code: int


class SetupFailure(StrEnum):
    """Чем сорвалась подготовка вызова до запуска тела."""

    NONE = ""
    CHAIN_LOST = "chain_lost"
    """fuse-демон корня секции мёртв: виноват не вызов, а секция."""
    MOUNT_ERROR = "mount_error"
    """Образ вызова не смонтирован."""


class CallSetupFailed(BaseModel):
    """Отчёт исполнителя о сорванной подготовке: едет по control-сокету.

    Тело инструмента этот сокет не получает — он закрывается до его запуска.
    Раньше о том же говорили метки в stderr, и любое тело могло их напечатать:
    поддельная метка `sandbox-chain-lost:` роняла зиготу всей секции.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "setup_failed"
    call_id: str = Field(min_length=1)
    reason: SetupFailure
    detail: str = ""


class WarmupCall(BaseModel):
    """Один прогрев: чей хук звать и с каким конфигом."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    config: dict[str, Any]


class WarmupMessage(BaseModel):
    """Первое сообщение хоста: прогревы модулей, объявленные их авторами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: str = "warmup"
    calls: tuple[WarmupCall, ...]


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
    def detach(cls, target: str) -> None:
        """Отцепить точку от дерева вызова: тело её уже не увидит."""
        rc = cls.libc().umount2(target.encode(), int(MountFlag.DETACH))
        if rc != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"umount2({target}) failed: {os.strerror(errno)}")

    class _CloneArgs(ctypes.Structure):
        """Аргументы clone3(2) в порядке ядра; хвост не используется."""

        _fields_: ClassVar = [
            ("flags", ctypes.c_uint64),
            ("pidfd", ctypes.c_uint64),
            ("child_tid", ctypes.c_uint64),
            ("parent_tid", ctypes.c_uint64),
            ("exit_signal", ctypes.c_uint64),
            ("stack", ctypes.c_uint64),
            ("stack_size", ctypes.c_uint64),
            ("tls", ctypes.c_uint64),
            ("set_tid", ctypes.c_uint64),
            ("set_tid_size", ctypes.c_uint64),
            ("cgroup", ctypes.c_uint64),
        ]

    SYS_CLONE3: ClassVar[int] = 435
    CHILD_EXIT_SIGNAL: ClassVar[int] = 17
    """SIGCHLD: без него родитель не дождётся ребёнка обычным waitpid."""

    @classmethod
    def clone_into(cls, flags: int, cgroup_fd: int) -> int:
        """clone3 с рождением ребёнка в готовом cgroup: 0 — это ребёнок.

        Миграция уже работающего процесса в leaf стоит десятки миллисекунд на
        каждый вызов; рождение внутри leaf'а обходится в доли миллисекунды.
        Звать только из однопоточного процесса: послефорковое обслуживание
        интерпретатора здесь не выполняется.
        """
        args = cls._CloneArgs(
            flags=flags | CloneFlag.INTO_CGROUP,
            exit_signal=cls.CHILD_EXIT_SIGNAL,
            cgroup=cgroup_fd,
        )
        pid = cls.libc().syscall(
            cls.SYS_CLONE3, ctypes.byref(args), ctypes.sizeof(args)
        )
        if pid < 0:
            code = ctypes.get_errno()
            raise OSError(code, f"clone3(into cgroup): {os.strerror(code)}")

        return pid

    CGROUP_PROCS: ClassVar[str] = "cgroup.procs"
    SELF_PID: ClassVar[bytes] = b"0"

    @classmethod
    def join_cgroup(cls, cgroup_fd: int) -> None:
        """Вписать себя в leaf через его дескриптор: cgroupfs внутри не смонтирован."""
        fd = os.open(cls.CGROUP_PROCS, os.O_WRONLY, dir_fd=cgroup_fd)
        try:
            os.write(fd, cls.SELF_PID)
        finally:
            os.close(fd)

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

    LAST_CAP: ClassVar[str] = "/proc/sys/kernel/cap_last_cap"

    @classmethod
    def drop_capabilities(cls) -> None:
        """Тело остаётся без прав: bounding set, no_new_privs и сам capset.

        Один capset обнуляет только наборы процесса, а bounding set остаётся
        от зиготы (CAP_SYS_ADMIN, CAP_SYS_RESOURCE) — по нему права можно
        вернуть, если тело когда-нибудь получит исполняемый файл с
        capabilities или своё пространство пользователей.
        """
        cls._deny_new_privileges()
        cls._drop_bounding_set()

        header = cls._CapHeader(cls._CAP_VERSION_3, 0)
        data = (cls._CapData * 2)()
        if cls.libc().capset(ctypes.byref(header), ctypes.byref(data)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"capset: {os.strerror(code)}")

    PR_SET_NO_NEW_PRIVS: ClassVar[int] = 38
    PR_CAPBSET_DROP: ClassVar[int] = 24

    @classmethod
    def _deny_new_privileges(cls) -> None:
        rc = cls.libc().prctl(cls.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        if rc != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"prctl(NO_NEW_PRIVS): {os.strerror(code)}")

    @classmethod
    def _drop_bounding_set(cls) -> None:
        with open(cls.LAST_CAP) as f:
            last = int(f.read().strip())

        for cap in range(last + 1):
            rc = cls.libc().prctl(cls.PR_CAPBSET_DROP, cap, 0, 0, 0)
            if rc == 0:
                continue

            code = ctypes.get_errno()
            raise OSError(code, f"prctl(CAPBSET_DROP, {cap}): {os.strerror(code)}")

    @classmethod
    def enter_call_namespaces(cls, mounts: CallMounts) -> None:
        """Своё дерево монтирований вызова; NEWPID ставит вызывающий.

        Точки берутся из профиля: пустое значение означает, что профиль такой
        точки не объявил и приватной её делать не из чего.
        """
        cls.mount("none", "/", "", MountFlag.REC | MountFlag.PRIVATE, "")

        if mounts.proc:
            cls.mount(
                "proc", mounts.proc, "proc", MountFlag.NOSUID | MountFlag.NODEV, ""
            )

        if mounts.tmp:
            cls.mount(
                "tmpfs",
                mounts.tmp,
                "tmpfs",
                MountFlag.NOSUID | MountFlag.NODEV,
                f"size={mounts.tmp_bytes}",
            )


class ZygoteMain:
    """Главный цикл зиготы: прогрев, приём запросов, fork и учёт детей."""

    USERNS_SYSCTL: ClassVar[str] = "/proc/sys/user/max_user_namespaces"
    """Вложенные userns запрещаются до первого вызова, как в цепочке лаунчера."""

    def __init__(
        self, sock: socket.socket, tools: Sequence[ToolLike], reap_poll_sec: float
    ) -> None:
        self._reap_poll_sec = reap_poll_sec
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
    def run(cls, args: ZygoteArgs) -> int:
        """Вход процесса: прогрев модулей, ready, цикл обслуживания."""
        PayloadLogging.setup(args.log_level)

        sock = socket.socket(fileno=args.socket_fd)

        module_names = args.modules

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

        main = cls(sock, tools, args.reap_poll_sec)
        ZygoteWire.send(sock, ZygoteReady(warmup_ms=warmup.ms()))

        return main.serve()

    @classmethod
    def _run_warmups(cls, sock: socket.socket, modules: dict[str, ModuleType]) -> None:
        """Первое сообщение хоста — прогревы; они исполняются до ready.

        Хуки объявлены авторами инструментов через @warmup и лежат в реестре
        фасада; хост присылает конфиг на каждый. Объявленный хук без конфига —
        нарушение контракта: молча пропустить его нельзя, иначе прогрев
        случался бы на каждом вызове.
        """
        message, _fds = ZygoteWire.recv(sock)
        warmup = WarmupMessage.model_validate(message)

        sent = {(call.module, call.hook): call for call in warmup.calls}

        for name in modules:
            for hook in WarmupHooks.of(name):
                call = sent.pop((name, hook.name), None)
                if call is None:
                    msg = (
                        f"module {name} declares warmup {hook.name!r}, but the "
                        f"host sent no config for it"
                    )
                    raise ZygoteProtocolError(msg)

                elapsed = Elapsed()
                asyncio.run(hook.body(hook.config_model.model_validate(call.config)))
                logger.info(
                    "zygote: %s.%s warmed up in %dms", name, hook.name, elapsed.ms()
                )

        for module, hook_name in sent:
            msg = (
                f"host sent warmup {hook_name!r} for module {module}, but no such "
                f"hook is declared there"
            )
            raise ZygoteProtocolError(msg)

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
                [self._sock, self._sigchld_r], [], [], self._reap_poll_sec
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

        expected = CallFd.count()
        if request.into_cgroup:
            expected += 1

        if len(fds) != expected:
            for fd in fds:
                os.close(fd)
            msg = f"call {request.call_id}: expected {expected} fds, got {len(fds)}"
            raise ZygoteProtocolError(msg)

        control = socket.socket(fileno=fds[CallFd.CONTROL])

        timing = SetupTiming()
        pid = os.fork()
        if pid == 0:
            self._child(request, fds, timing)
            os._exit(127)

        for index in (CallFd.STDIN, CallFd.STDOUT, CallFd.STDERR, CallFd.RESULT):
            os.close(fds[index])

        if request.into_cgroup:
            os.close(fds[CallFd.CGROUP])

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

    def _child(self, request: CallRequest, fds: list[int], timing: SetupTiming) -> None:
        """Первый форк: изоляция namespace'ов и второй форк под NEWPID."""
        try:
            timing.mark("fork")
            self._sock.close()
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            os.close(self._sigchld_r)
            os.close(self._sigchld_w)

            if request.isolate:
                Isolation.unshare(CloneFlag.NEWNS | CloneFlag.NEWIPC | CloneFlag.NEWUTS)

            timing.mark("unshare")

            pid, join_self = self._spawn_executor(request, fds)
            if pid != 0:
                _, status = os.waitpid(pid, 0)
                os._exit(os.waitstatus_to_exitcode(status) & 0xFF)

            if join_self:
                Isolation.join_cgroup(fds[CallFd.CGROUP])

            timing.mark("fork2")
            self._grandchild(request, fds, timing)
        except BaseException as exc:
            self._report_failure(request, fds, exc)
            os._exit(126)

    @classmethod
    def _report_failure(
        cls, request: CallRequest, fds: list[int], exc: BaseException
    ) -> None:
        """Сбой подготовки: причина уходит хосту control-сокетом, а не в stderr.

        ENOTCONN означает, что fuse-демон корня секции мёртв: вызов не виноват
        и повторять его бессмысленно, пока хост не поднимет зиготу заново.
        Сообщение об этом хост принимает только с control-сокета, до которого
        телу инструмента не дотянуться.
        """
        print(f"zygote child failed: {exc}", file=sys.stderr, flush=True)  # noqa: T201

        if not isinstance(exc, OSError):
            return

        try:
            control = socket.socket(fileno=fds[CallFd.CONTROL])
        except OSError:
            return

        cls._fail_setup(control, request, cls._failure_of(exc), str(exc))

    @staticmethod
    def _spawn_executor(request: CallRequest, fds: list[int]) -> tuple[int, bool]:
        """Исполнитель вызова: pid namespace всегда, cgroup — если приехал fd.

        Отдаёт pid и признак «вписаться в leaf самому»: рождение в cgroup
        избавляет от миграции работающего процесса, которая стоит десятки
        миллисекунд, но при отказе ядра остаётся запись через тот же
        дескриптор. Форк здесь однопоточный — первый форк зиготы отсёк её
        потоки, поэтому clone3 без послефоркового обслуживания безопасен.
        """
        if not request.isolate:
            return os.fork(), False

        if request.into_cgroup:
            try:
                return Isolation.clone_into(CloneFlag.NEWPID, fds[CallFd.CGROUP]), False
            except OSError as exc:
                trace(f"clone3 into cgroup refused ({exc}), falling back to fork")

        Isolation.unshare(CloneFlag.NEWPID)

        return os.fork(), request.into_cgroup

    @classmethod
    def _check_root(cls, control: socket.socket, request: CallRequest) -> None:
        """Корень секции жив? Ответ нужен, пока control ещё у исполнителя.

        Мёртвый fuse-демон корня виден только при обращении к файлам, а тело
        обращается к ним уже после закрытия control: без этой проверки отказ
        выглядел бы обычной ошибкой инструмента, и секция не восстановилась
        бы. Одна операция над корнем стоит микросекунды.
        """
        try:
            os.stat("/")
        except OSError as exc:
            cls._fail_setup(control, request, cls._failure_of(exc), str(exc))
            os._exit(MountError.EXIT_CODE)

    @staticmethod
    def _failure_of(exc: OSError) -> SetupFailure:
        """ENOTCONN на монтировании — мёртвый fuse-демон корня секции."""
        if exc.errno == errno.ENOTCONN:
            return SetupFailure.CHAIN_LOST

        return SetupFailure.MOUNT_ERROR

    @staticmethod
    def _fail_setup(
        control: socket.socket,
        request: CallRequest,
        reason: SetupFailure,
        detail: str,
    ) -> None:
        """Отчёт о сорванной подготовке хосту; молчание тут не оставляем."""
        report = CallSetupFailed(call_id=request.call_id, reason=reason, detail=detail)
        try:
            ZygoteWire.send(control, report)
        except OSError as exc:
            print(  # noqa: T201
                f"zygote setup report not delivered: {exc}", file=sys.stderr, flush=True
            )

        control.close()

    @staticmethod
    def _close_inherited(request: CallRequest, fds: list[int]) -> None:
        """Закрыть всё, что телу не принадлежит: leaf cgroup и копии каналов.

        Дескриптор leaf'а даёт запись в memory.max и обход соседних групп,
        поэтому переживать вход в cgroup он не должен.
        """
        if request.into_cgroup:
            os.close(fds[CallFd.CGROUP])

        for index in (CallFd.STDIN, CallFd.STDOUT, CallFd.STDERR):
            fd = fds[index]
            if fd in (0, 1, 2):
                continue

            os.close(fd)

    def _grandchild(
        self, request: CallRequest, fds: list[int], timing: SetupTiming
    ) -> None:
        """Исполнитель: приватные /proc и /tmp, cgroup через хост, образы, тело.

        Порядок жёсткий: handshake до монтирования (fuse2fs-демон рождается
        уже в cgroup-leaf вызова), монтирование до сброса capabilities.
        """
        if request.isolate:
            Isolation.enter_call_namespaces(request.mounts)

        timing.mark("namespaces")

        os.dup2(fds[CallFd.STDIN], 0)
        os.dup2(fds[CallFd.STDOUT], 1)
        os.dup2(fds[CallFd.STDERR], 2)
        os.environ[ToolChannel.RESULT.env_name] = str(fds[CallFd.RESULT])

        self._close_inherited(request, fds)

        control = socket.socket(fileno=fds[CallFd.CONTROL])
        self._handshake(control)
        timing.mark("handshake")

        # control живёт до конца подготовки: по нему уходит отчёт о сбое,
        # которому нельзя дать подделать телу инструмента
        mounts = _CallMounts.of(request)
        try:
            mounts.mount()
        except MountError as exc:
            self._fail_setup(control, request, SetupFailure.MOUNT_ERROR, str(exc))
            os._exit(MountError.EXIT_CODE)
        except OSError as exc:
            self._fail_setup(control, request, self._failure_of(exc), str(exc))
            os._exit(MountError.EXIT_CODE)

        timing.mark("images")

        request.limits.apply()

        if request.isolate:
            Isolation.drop_capabilities()

        if request.cwd:
            os.chdir(request.cwd)

        self._check_root(control, request)
        control.close()

        timing.mark("limits")
        timing.report(request.call_id)

        timing = SetupTiming()
        try:
            code = self._run_body(request)
            timing.mark("body")
        finally:
            # записи доезжают до образа только после штатного выхода fuse2fs
            mounts.shutdown()

        timing.mark("unmount")
        timing.report(request.call_id, phase="teardown")

        os._exit(code)

    def _run_body(self, request: CallRequest) -> int:
        """Тело своим процессом — только когда после него надо гасить демон.

        Исполнитель обязан пережить тело, если у вызова есть образ: записи
        доезжают до образа лишь после штатного выхода fuse2fs. Без образов
        ждать нечего, а лишний форк прогретой зиготы стоит десяток
        миллисекунд, поэтому тело исполняется прямо здесь.
        """
        if not request.images:
            return self._body_here(request)

        pid = os.fork()
        if pid == 0:
            self._body(request)

        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    def _body_here(self, request: CallRequest) -> int:
        """Тело в самом исполнителе: shell замещает процесс, модуль отдаёт код."""
        argv = list(request.argv)
        if request.kind is CallKind.SHELL:
            self._flush_streams()
            os.execv(argv[0], argv)  # noqa: S606 — argv собран хостом, без shell

        code = ToolMain.run(self._tools, argv)
        self._flush_streams()

        return code

    def _body(self, request: CallRequest) -> None:
        # тело умирает вместе с исполнителем и без pid ns: в изоляции это и
        # так гарантирует init, в голом запуске — только pdeathsig
        FuseMounter.set_pdeathsig()

        argv = list(request.argv)
        if request.kind is CallKind.SHELL:
            self._flush_streams()
            os.execv(argv[0], argv)  # noqa: S606 — argv собран хостом, без shell

        code = ToolMain.run(self._tools, argv)

        # os._exit не сбрасывает буферы: печать тела иначе не доедет до канала
        self._flush_streams()

        os._exit(code)

    @staticmethod
    def _flush_streams() -> None:
        sys.stdout.flush()
        sys.stderr.flush()

    @staticmethod
    def _handshake(control: socket.socket) -> None:
        """Хост узнаёт host-pid исполнителя: он нужен ему для kill и таймаута.

        Ответа ждать нечего: в cgroup-leaf исполнитель попадает сам — рождением
        внутри него либо записью через его дескриптор.
        """
        creds = array.array("i", [os.getpid(), os.getuid(), os.getgid()])
        control.sendmsg(
            [ControlMark.BORN.bytes()],
            [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, creds.tobytes())],
        )


class _CallMounts:
    """rw-образы одного вызова: копия шаблона под локом, fuse2fs на точку."""

    def __init__(
        self,
        images: Sequence[ImageMount],
        stores: Mapping[str, ImageStore],
        mounter: FuseMounter | None,
        staging: Sequence[str],
    ) -> None:
        self._images = tuple(images)
        self._stores = dict(stores)
        self._mounter = mounter
        self._staging = tuple(staging)

    @classmethod
    def of(cls, request: CallRequest) -> _CallMounts:
        if not request.images:
            return cls((), {}, None, request.staging)

        mounting = request.mounting
        if mounting is None:
            msg = "call carries images but no mounting parameters"
            raise ZygoteProtocolError(msg)

        options = mounting.options()

        # у каждого образа свой шаблон: workspace копируется с одного, а
        # прочие rw_images профиля — со своего
        stores: dict[str, ImageStore] = {}
        for spec in request.images:
            stores[spec.image] = ImageStore(
                spec.template,
                SparseCopier(options.copy_chunk_bytes),
                options.lock_wait_sec,
            )

        binaries = TrustedBinaries(dirs=(mounting.fuse2fs_dir,))
        mounter = FuseMounter(options, binaries, pass_fds=())
        return cls(request.images, stores, mounter, request.staging)

    def mount(self) -> None:
        if self._mounter is not None:
            self._mount_images()

        self._detach_staging()

    def _mount_images(self) -> None:
        if self._mounter is None:
            return

        for spec in self._images:
            self._stores[spec.image].acquire(spec.image)

        for spec in self._images:
            self._mounter.mount(spec.image, spec.target, readonly=False)

    def _detach_staging(self) -> None:
        """Обвязка монтирования уходит из вида тела в любом вызове.

        Профиль без образов её всё равно получает от зиготы: без этого шага
        телу оставался каталог с образами всех пользователей.
        """
        for path in self._staging:
            if not FuseMounter.is_mounted(path):
                continue

            Isolation.detach(path)

    def shutdown(self) -> None:
        if self._mounter is not None:
            self._mounter.shutdown()

        for store in self._stores.values():
            store.release_all()


def main(argv: Sequence[str]) -> int:
    """Аргументы запуска зиготы; модули — тела инструментов её секции."""
    return ZygoteMain.run(ZygoteArgs.parse(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
