"""Опыты над внешним слоем цепочки лаунчера внутри слоя теста.

Запускает его test_launcher_view в слое PrivateImageMount.racing: своё
пространство монтирования с shared-точками, хосту невидимое. В пространстве
монтирования хоста скрипт работать отказывается: точка, которую он
монтирует и снимает, роняла бы старты песочниц всей машины.

race — точка FUSE вне вида монтируется и снимается в цикле, а операции
хранилища образов стартуют цепочку снова и снова; итог — число стартов и
сбоев.
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
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from boba.canvas.storage import StorageError
from boba.runtime.config import AppLayers
from boba.runtime.storage import ImageStorageClient, LocalStorageConfig
from boba.sandbox import WorkspaceSpec
from boba.sandbox.argv import ZygoteArgv
from boba.stand.shell import ShellRun
from boba.stand.zygote import ProcTree, SandboxStand, ZygoteStand
from boba.workspace.binaries import SandboxBinary, TrustedBinaries
from boba.workspace.launcher import MountingConfig


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


class StorageRace:
    """Операции хранилища образов, пока точка вне вида мигает."""

    KEY: ClassVar[str] = "7/t1/upload/race.txt"

    def __init__(self, work: Path, flapper: PointFlapper) -> None:
        images = work / "ws"
        images.mkdir(exist_ok=True)
        config = LocalStorageConfig(
            kind="image",
            mount_dir="/tmp",  # noqa: S108 — внутри слоя цепочки
            workspace=WorkspaceSpec.model_validate(
                {
                    "template": SandboxStand.mkfs_template(work),
                    "mount": f"{images}/{{user_id}}.ext4:/workspace",
                }
            ),
            op_timeout_sec=60,
            mounting=MountingConfig(
                mount_wait_sec=10.0,
                mount_poll_sec=0.01,
                shutdown_wait_sec=5.0,
                lock_wait_sec=30.0,
                copy_chunk_bytes=1 << 16,
            ),
            binaries=TrustedBinaries(dirs=tuple(SandboxStand.bin_dirs())),
            interpreter=AppLayers.interpreter(),
        )
        self._client = ImageStorageClient(config)
        self._flapper = flapper
        self._starts = 0
        self._failures: list[str] = []

    async def run(self, seconds: float, workers: int) -> RaceReport:
        await self._client.upload_stream(self.KEY, self._source())

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
            try:
                await self._client.stat(self.KEY)
            except StorageError as exc:
                self._failures.append(str(exc)[:300])

    @staticmethod
    async def _source() -> AsyncIterator[bytes]:
        yield b"race\n"


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
            race = StorageRace(args.work, flapper)
            report = await race.run(args.seconds, args.workers)
            return json.dumps(asdict(report))

        return json.dumps(asdict(ResolvedProbe().run()))


async def main() -> None:
    print(await LayerCli().run(sys.argv[1:]))


if __name__ == "__main__":
    asyncio.run(main())
