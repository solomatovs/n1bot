"""Опыты над внешним слоем цепочки лаунчера внутри слоя теста.

Запускает его test_launcher_view в слое PrivateImageMount.racing: своё
пространство монтирования с shared-точками, хосту невидимое. В пространстве
монтирования хоста скрипт работать отказывается: точка, которую он
монтирует и снимает, роняла бы старты песочниц всей машины.

race — точка FUSE вне вида монтируется и снимается в цикле, а цепочка
лаунчера над образом стартует снова и снова; итог — число стартов и сбоев.
resolved — в слое /etc/resolv.conf — ссылка systemd-resolved на файл в /run;
зигота с биндом /etc/resolv.conf отдаёт, что видит тело и внешний слой.

Итог печатается одной строкой JSON в stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from boba.runtime.config import ProcessInterpreter
from boba.sandbox.argv import ZygoteArgv
from boba.stand.shell import ShellRun
from boba.stand.zygote import ProcTree, SandboxStand, ZygoteStand
from boba.workspace.binaries import SandboxBinary, TrustedBinaries
from boba.workspace.launcher import (
    ImageMountPoint,
    LauncherChain,
    LauncherExit,
    LauncherMode,
    LayerSources,
    MountingConfig,
    ResourceLimits,
)


class HostNamespace:
    """Сверка с пространством монтирования хоста: в нём опыт запрещён."""

    OWN: ClassVar[str] = "/proc/self/ns/mnt"

    def __init__(self, host_inode: int) -> None:
        self._host = host_inode

    def require_private(self) -> None:
        own = os.stat(self.OWN).st_ino
        if own == self._host:
            msg = (
                f"launcher layer: refusing to mount in the host mount namespace "
                f"(inode {own}); run me through PrivateImageMount.racing"
            )
            raise SystemExit(msg)


class Libc:
    """mount(2) и umount2(2) слоя: монтирует и снимает только внутри него."""

    MS_NOSUID: ClassVar[int] = 2
    MS_NODEV: ClassVar[int] = 4

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)

    def tmpfs(self, point: Path) -> None:
        flags = self.MS_NOSUID | self.MS_NODEV
        if self._libc.mount(b"tmpfs", bytes(point), b"tmpfs", flags, None) != 0:
            code = ctypes.get_errno()
            msg = f"mount tmpfs at {point} in the test layer: {os.strerror(code)}"
            raise OSError(code, msg)

    def umount(self, point: Path) -> int:
        """0 — снята, иначе errno отказа."""
        if self._libc.umount2(bytes(point), 0) == 0:
            return 0

        return ctypes.get_errno()


@dataclass(frozen=True)
class RaceReport:
    starts: int
    failures: tuple[str, ...]
    mount_cycles: int
    umount_errors: int


class PointFlapper:
    """Точка FUSE вне вида, которую слой монтирует и снимает раз за разом.

    Пауза между циклами держит темп около сотни в секунду: без неё снятия
    занимают блокировку монтирования ядра так плотно, что старты цепочек
    стоят в очереди за ней.
    """

    PAUSE_SEC: ClassVar[float] = 0.01

    def __init__(self, image: Path, point: Path) -> None:
        self._image = image
        self._point = point
        self._libc = Libc()
        self._stop = threading.Event()
        self._cycles = 0
        self._umount_errors = 0
        self._thread = threading.Thread(target=self._loop, name="point-flapper")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> tuple[int, int]:
        self._stop.set()
        self._thread.join()
        return self._cycles, self._umount_errors

    def _loop(self) -> None:
        fuse2fs = SandboxStand.fuse2fs()
        argv = [fuse2fs, "-o", "ro", str(self._image), str(self._point)]
        while not self._stop.is_set():
            done = subprocess.run(argv, capture_output=True, check=False)
            if done.returncode != 0:
                continue

            self._cycles += 1
            if self._libc.umount(self._point) != 0:
                self._umount_errors += 1

            time.sleep(self.PAUSE_SEC)


class ChainRace:
    """Старты цепочки лаунчера над образом, пока точка вне вида мигает.

    Каждая операция — полный старт внешнего слоя: тот самый шаг, на котором
    старый вид с корнем хоста падал на снятой точке. Сбой — любой код выхода,
    кроме OK.
    """

    REL: ClassVar[str] = "race.txt"
    MOUNT_DIR: ClassVar[str] = "/tmp"  # noqa: S108 — внутри слоя цепочки
    PAYLOAD: ClassVar[bytes] = b"race\n"
    DETAIL_CHARS: ClassVar[int] = 300

    def __init__(self, work: Path, flapper: PointFlapper) -> None:
        images = work / "ws"
        images.mkdir(exist_ok=True)
        self._image = str(images / "7.ext4")
        self._template = SandboxStand.mkfs_template(work)
        mounting = MountingConfig(
            mount_wait_sec=10.0,
            mount_poll_sec=0.01,
            shutdown_wait_sec=5.0,
            lock_wait_sec=30.0,
            copy_chunk_bytes=1 << 16,
        )
        self._chain = LauncherChain(
            ProcessInterpreter().snapshot(),
            TrustedBinaries(dirs=tuple(SandboxStand.bin_dirs())),
            mounting.to_options(),
        )
        self._flapper = flapper
        self._starts = 0
        self._failures: list[str] = []

    async def run(self, seconds: float, workers: int) -> RaceReport:
        code, detail = await self._launch(LauncherMode.WRITE, self.PAYLOAD)
        if code != LauncherExit.OK:
            msg = (
                f"race: write of {self.REL} into {self._image} "
                f"exited with {code}: {detail}"
            )
            raise SystemExit(msg)

        self._flapper.start()
        deadline = time.monotonic() + seconds
        try:
            loops: list[asyncio.Task[None]] = []
            for _ in range(workers):
                loops.append(asyncio.create_task(self._stat_until(deadline)))

            await asyncio.gather(*loops)
        finally:
            cycles, umount_errors = self._flapper.stop()

        return RaceReport(
            starts=self._starts,
            failures=tuple(self._failures),
            mount_cycles=cycles,
            umount_errors=umount_errors,
        )

    async def _stat_until(self, deadline: float) -> None:
        while time.monotonic() < deadline:
            self._starts += 1
            code, detail = await self._launch(LauncherMode.STAT, b"")
            if code != LauncherExit.OK:
                self._failures.append(f"stat exited with {code}: {detail}")

    async def _launch(self, mode: LauncherMode, payload: bytes) -> tuple[int, str]:
        argv = self._chain.argv(
            images=[(self._image, ImageMountPoint.under(self.MOUNT_DIR, self._image))],
            ro_images=(),
            template=self._template,
            op=[mode.value, self.REL],
            limits=ResourceLimits(),
            pass_fds=(),
            nested=LayerSources(ro=(), rw=()),
            network=False,
            replace=False,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate(payload)

        code = proc.returncode
        if code is None:
            msg = f"race: {mode.value} of {self.REL} ended without an exit code"
            raise SystemExit(msg)

        detail = err.decode(errors="replace").strip()[: self.DETAIL_CHARS]
        return code, detail


@dataclass(frozen=True)
class ResolvedReport:
    body: str
    sources: tuple[str, ...]
    points: tuple[str, ...]


class ResolvedLayout:
    """Раскладка systemd-resolved внутри слоя: /etc/resolv.conf — ссылка на
    файл в /run.

    /etc и /run слоя накрыты своими tmpfs: ссылка и файл появляются только в
    слое, а из /etc машины в его tmpfs копируется то, что читают процессы
    цепочки.
    """

    LINK: ClassVar[str] = "../run/systemd/resolve/stub-resolv.conf"
    CONTENT: ClassVar[str] = "nameserver 192.0.2.53\n"
    ETC: ClassVar[Path] = Path("/etc")
    RUN: ClassVar[Path] = Path("/run")
    KEPT: ClassVar[tuple[str, ...]] = (
        "ld.so.cache",
        "hosts",
        "passwd",
        "group",
        "nsswitch.conf",
    )

    def __init__(self) -> None:
        self._libc = Libc()

    def make(self) -> None:
        kept: dict[str, bytes] = {}
        for name in self.KEPT:
            source = self.ETC / name
            if source.exists():
                kept[name] = source.read_bytes()

        self._libc.tmpfs(self.RUN)
        stub = self.RUN / "systemd/resolve/stub-resolv.conf"
        stub.parent.mkdir(parents=True)
        stub.write_text(self.CONTENT, encoding="utf-8")

        self._libc.tmpfs(self.ETC)
        for name, body in kept.items():
            (self.ETC / name).write_bytes(body)

        os.symlink(self.LINK, self.ETC / "resolv.conf")


class ResolvedProbe:
    """Зигота с биндом /etc/resolv.conf, как у плагинов с сетью: что видит
    тело и внешний слой её цепочки."""

    BIND: ClassVar[str] = "/etc/resolv.conf:/etc/resolv.conf"

    def __init__(self) -> None:
        self._layout = ResolvedLayout()
        self._stand = ZygoteStand()

    def run(self) -> ResolvedReport:
        self._layout.make()
        profile = SandboxStand.profile(ro=(*SandboxStand.image_ro_binds(), self.BIND))
        try:
            caller = self._stand.caller("resolved", profile)
            body = ShellRun.call_text(caller, "cat /etc/resolv.conf")
            if body.exit_code != 0:
                msg = f"cat /etc/resolv.conf in the body: rc={body.exit_code}"
                raise SystemExit(msg)

            roots = ProcTree().named(caller.supervisor.pid, SandboxBinary.FUSE2FS)
            points = self._points(next(iter(roots)))
        finally:
            self._stand.stop()

        return ResolvedReport(
            body=body.stdout,
            sources=ZygoteArgv(profile).sources().ro,
            points=points,
        )

    @staticmethod
    def _points(pid: int) -> tuple[str, ...]:
        points: list[str] = []
        mountinfo = Path("/proc") / str(pid) / "mountinfo"
        for line in mountinfo.read_text().splitlines():
            points.append(line.split()[4])

        return tuple(points)


class LayerCli:
    """Разбор аргументов и запуск опыта."""

    def __init__(self) -> None:
        parser = argparse.ArgumentParser(prog="launcher_layer")
        parser.add_argument("--host-mntns", type=int, required=True)
        commands = parser.add_subparsers(dest="command", required=True)

        race = commands.add_parser("race")
        race.add_argument("--image", type=Path, required=True)
        race.add_argument("--point", type=Path, required=True)
        race.add_argument("--work", type=Path, required=True)
        race.add_argument("--seconds", type=float, required=True)
        race.add_argument("--workers", type=int, required=True)

        commands.add_parser("resolved")

        self._parser = parser

    async def run(self, argv: Sequence[str]) -> str:
        args = self._parser.parse_args(argv)
        HostNamespace(args.host_mntns).require_private()

        if args.command == "race":
            flapper = PointFlapper(args.image, args.point)
            race = ChainRace(args.work, flapper)
            report = await race.run(args.seconds, args.workers)
            return json.dumps(asdict(report))

        return json.dumps(asdict(ResolvedProbe().run()))


async def main() -> None:
    print(await LayerCli().run(sys.argv[1:]))


if __name__ == "__main__":
    asyncio.run(main())
