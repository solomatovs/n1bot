"""Вид внешнего слоя цепочки лаунчера на живом старте.

Внешний слой видит не корень хоста, а явный список путей (LauncherView).
Таблицу монтирования слоя читаем у процесса, который живёт в нём: fuse2fs,
смонтировавшего образ. Опыты с исчезающей точкой и ссылкой resolv.conf идут
в собственном слое теста (PrivateImageMount.racing): хост их точек не видит.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from launcher_layer import ResolvedLayout, ResolvedProbe

from boba.runtime.config import ProgramRoots
from boba.runtime.storage import ImageStorageClient, LocalStorageConfig
from boba.sandbox import WorkspaceSpec
from boba.sandbox.argv import ZygoteArgv
from boba.sandbox.profile import SandboxMount, SandboxProfile
from boba.sandbox.zygote import ZygoteToolCaller
from boba.stand.shell import ShellRun
from boba.stand.zygote import (
    ROOTFS_IMAGE,
    PrivateImageMount,
    ProcTree,
    SandboxStand,
    ZygoteStand,
)
from boba.workspace.binaries import SandboxBinary
from boba.workspace.launcher import ImageMountPoint, LayerPath, MountingConfig

needs_chain = pytest.mark.failif(
    not ROOTFS_IMAGE.exists()
    or not SandboxStand.FUSE2FS.exists()
    or not os.path.exists(LayerPath.FUSE),
    reason="нужны rootfs.ext4 shell-плагина, fuse2fs развёртывания и /dev/fuse",
)


class MountTable:
    """Точки монтирования пространства процесса по /proc/<pid>/mountinfo."""

    ESCAPE: ClassVar[re.Pattern[str]] = re.compile(r"\\([0-7]{3})")

    def __init__(self, pid: int) -> None:
        self._path = Path("/proc") / str(pid) / "mountinfo"

    def points(self) -> list[str]:
        points: list[str] = []
        for line in self._path.read_text().splitlines():
            points.append(self._unescaped(line.split()[4]))

        return points

    def _unescaped(self, field: str) -> str:
        return self.ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


class LayerArgv:
    """Что внешнему слою велено смонтировать: бинды argv его bwrap."""

    BINDS: ClassVar[tuple[str, ...]] = ("--ro-bind", "--bind")

    def __init__(self, pid: int) -> None:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        self._argv = raw.rstrip(b"\0").decode().split("\0")

    def binds(self) -> list[str]:
        head = self._argv[: self._argv.index("--")]
        found: list[str] = []
        for index, arg in enumerate(head):
            if arg in self.BINDS:
                found.append(head[index + 2])

        return found


class LayerView:
    """Проверки таблицы внешнего слоя против того, что ему велено."""

    OWN: ClassVar[tuple[str, ...]] = (
        LayerPath.ROOT.value,
        LayerPath.PROC.value,
        SandboxMount.TMP.value,
    )
    """Точки, которые слой ставит сам, кроме /dev: корень, procfs, tmpfs."""

    HIDDEN: ClassVar[tuple[str, ...]] = ("/sys", "/run", "/home", "/root", "/boot")

    def __init__(self, points: Sequence[str], declared: Sequence[str]) -> None:
        self._points = points
        self._declared = declared

    def unexpected(self, mounted: Sequence[str]) -> list[str]:
        """Точки сверх объявленных argv, своих и смонтированных образов."""
        allowed = {*self._declared, *self.OWN, *mounted}
        extra: list[str] = []
        for point in self._points:
            if point in allowed:
                continue

            if self._under(LayerPath.DEV.value, point):
                continue

            extra.append(point)

        return extra

    def missing(self) -> list[str]:
        """Объявленные бинды, которых в таблице нет."""
        absent: list[str] = []
        for path in self._declared:
            if path not in self._points:
                absent.append(path)

        return absent

    def visible(self, path: str) -> bool:
        """Путь виден в слое: он сам или его предок — точка монтирования."""
        for point in self._points:
            if point == LayerPath.ROOT.value:
                continue

            if self._under(point, path):
                return True

        return False

    def leaked(self) -> list[str]:
        """Точки хоста, которых слою не видно ни при каком составе."""
        found: list[str] = []
        for point in self._points:
            for hidden in self.HIDDEN:
                if self._under(hidden, point):
                    found.append(point)

        return found

    @staticmethod
    def _under(root: str, path: str) -> bool:
        if path == root:
            return True

        return path.startswith(root.rstrip("/") + "/")


class Secrets:
    """Каталог в духе conf/krb: krb5.conf профиль биндит, keytab — нет."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "krb"
        self.dir.mkdir()
        self.krb5 = self.dir / "krb5.conf"
        self.krb5.write_text("[libdefaults]\n    default_realm = STAND.TEST\n")
        self.keytab = self.dir / "service.keytab"
        self.keytab.write_bytes(b"not a real keytab")


@pytest.fixture
def section(zygote_stand: ZygoteStand) -> Iterator[str]:
    """Уникальная секция на тест: зиготы реестра гасятся после него."""
    yield f"view-{uuid4().hex[:8]}"

    zygote_stand.stop()


@needs_chain
class TestZygoteLayer:
    """Слой зиготы: корень образом, workspace и бинды всех видов."""

    @staticmethod
    def _profile(tmp_path: Path, secrets: Secrets) -> SandboxProfile:
        rw = tmp_path / "rw"
        rw.mkdir()
        ro = (
            *SandboxStand.image_ro_binds(),
            "/etc/hosts:/etc/hosts",
            "/etc/resolv.conf:/etc/resolv.conf",
            f"{secrets.krb5}:/etc/krb5.conf",
        )
        return SandboxStand.image_profile(tmp_path, ro=ro, rw=(f"{rw}:/srv",))

    @staticmethod
    def _caller(
        zygote_stand: ZygoteStand, section: str, profile: SandboxProfile
    ) -> ZygoteToolCaller:
        return zygote_stand.caller(
            section, profile, path_vars=lambda: {"user_id": "7", "scope_id": "t1"}
        )

    def test_outer_layer_mounts_exactly_the_view(
        self, zygote_stand: ZygoteStand, section: str, tmp_path: Path
    ) -> None:
        secrets = Secrets(tmp_path)
        profile = self._profile(tmp_path, secrets)
        caller = self._caller(zygote_stand, section, profile)
        warm = ShellRun.call_text(caller, "true")
        if warm.exit_code != 0:
            raise AssertionError(f"зигота не отвечает: rc={warm.exit_code}")

        zygote = caller.supervisor.pid
        roots = ProcTree().named(zygote, SandboxBinary.FUSE2FS.value)
        if len(roots) != 1:
            raise AssertionError(f"ждали один fuse2fs корня у зиготы, нашли {roots}")

        points = MountTable(next(iter(roots))).points()
        declared = LayerArgv(zygote).binds()
        view = LayerView(points, declared)

        extra = view.unexpected((SandboxMount.ROOTFS.value,))
        if extra:
            raise AssertionError(f"в слое точки сверх вида: {extra}")

        if view.missing():
            raise AssertionError(f"объявленных биндов нет в слое: {view.missing()}")

        if view.leaked():
            raise AssertionError(f"слой видит каталоги хоста: {view.leaked()}")

        nested = ZygoteArgv(profile).sources()
        invisible: list[str] = []
        for source in (*nested.ro, *nested.rw):
            if os.path.exists(source) and not view.visible(source):
                invisible.append(source)

        if invisible:
            raise AssertionError(f"источники вложенного слоя не видны: {invisible}")

        if not view.visible(str(secrets.krb5)):
            raise AssertionError(f"krb5.conf {secrets.krb5} не виден слою")

        if view.visible(str(secrets.keytab)):
            raise AssertionError(f"keytab {secrets.keytab} виден слою")

        cwd = os.getcwd()
        if view.visible(cwd):
            raise AssertionError(f"текущий каталог процесса {cwd} виден слою")

    def test_body_sees_the_profile(
        self, zygote_stand: ZygoteStand, section: str, tmp_path: Path
    ) -> None:
        """Тело видит бинды профиля и ничего из вида внешнего слоя."""
        secrets = Secrets(tmp_path)
        profile = self._profile(tmp_path, secrets)
        caller = self._caller(zygote_stand, section, profile)

        table = ShellRun.call_text(caller, "cat /proc/self/mountinfo")
        if table.exit_code != 0:
            raise AssertionError(f"rc={table.exit_code}: {table.stderr}")

        body: set[str] = set()
        for line in table.stdout.splitlines():
            body.add(line.split()[4])

        targets: set[str] = set()
        for spec in profile.mounts.all_binds():
            targets.add(spec.target)

        if not targets <= body:
            raise AssertionError(f"тело не видит бинды профиля: {targets - body}")

        outer = {sys.prefix, str(SandboxStand.FUSE2FS), SandboxMount.SETUP.value}
        if outer & body:
            raise AssertionError(f"тело видит пути внешнего слоя: {outer & body}")

        krb5 = ShellRun.call_text(caller, "cat /etc/krb5.conf")
        if krb5.stdout != secrets.krb5.read_text():
            raise AssertionError(f"krb5.conf тела: {krb5.stdout!r}")

        written = ShellRun.call_text(caller, "echo body > /workspace/probe && echo ok")
        if written.stdout.strip() != "ok":
            raise AssertionError(f"запись в /workspace: {written.stderr}")


@needs_chain
class TestStorageLayer:
    """Слой операции хранилища: образ пользователя, шаблон, каталог образов."""

    KEY: ClassVar[str] = "7/t1/upload/held.txt"
    MOUNT_DIR: ClassVar[str] = "/tmp"  # noqa: S108 — tmpfs внутри слоя цепочки
    WAIT_SEC: ClassVar[float] = 30.0
    POLL_SEC: ClassVar[float] = 0.05

    def test_write_layer_mounts_exactly_the_view(self, tmp_path: Path) -> None:
        images = tmp_path / "ws"
        images.mkdir()
        template = SandboxStand.mkfs_template(tmp_path)
        client = ImageStorageClient(
            self._config(),
            self._workspace(images, template),
            ProgramRoots().locate(),
        )
        image = images / "7.ext4"

        points, declared = asyncio.run(self._held_write(client, image))
        view = LayerView(points, declared)

        mounted = (ImageMountPoint.under(self.MOUNT_DIR, str(image)),)
        extra = view.unexpected(mounted)
        if extra:
            raise AssertionError(f"в слое точки сверх вида: {extra}")

        if view.missing():
            raise AssertionError(f"объявленных биндов нет в слое: {view.missing()}")

        if view.leaked():
            raise AssertionError(f"слой видит каталоги хоста: {view.leaked()}")

        for path in (str(images), template):
            if not view.visible(path):
                raise AssertionError(f"{path} не виден слою хранилища")

        if view.visible(str(tmp_path)):
            raise AssertionError(f"слой видит весь {tmp_path}, а не каталог образов")

    async def _held_write(
        self, client: ImageStorageClient, image: Path
    ) -> tuple[list[str], list[str]]:
        """Запись висит на источнике, пока тест читает таблицу её слоя."""
        release = asyncio.Event()

        async def source() -> AsyncIterator[bytes]:
            yield b"held\n"
            await release.wait()

        upload = asyncio.create_task(client.upload_stream(self.KEY, source()))
        try:
            fuse = await asyncio.to_thread(self._fuse_of, image)
            points = MountTable(fuse).points()
            declared = LayerArgv(self._bwrap_of(fuse)).binds()
        finally:
            release.set()

        await upload
        return points, declared

    def _fuse_of(self, image: Path) -> int:
        """fuse2fs образа пользователя: он живёт в слое операции."""
        deadline = time.monotonic() + self.WAIT_SEC
        while time.monotonic() < deadline:
            for pid in self._fuse_pids():
                args = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                if str(image).encode() in args.split(b"\0"):
                    return pid

            time.sleep(self.POLL_SEC)

        msg = f"fuse2fs of {image} did not appear within {self.WAIT_SEC}s"
        raise AssertionError(msg)

    @staticmethod
    def _fuse_pids() -> Iterator[int]:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue

            try:
                comm = (entry / "comm").read_text().strip()
            except OSError:
                continue

            if comm == SandboxBinary.FUSE2FS.value:
                yield int(entry.name)

    PPID: ClassVar[re.Pattern[str]] = re.compile(r"^PPid:\s+(\d+)$", re.MULTILINE)

    def _bwrap_of(self, pid: int) -> int:
        """Внешний bwrap цепочки — ближайший предок fuse2fs с именем bwrap."""
        current = pid
        while current > 1:
            status = (Path("/proc") / str(current) / "status").read_text()
            found = self.PPID.search(status)
            if found is None:
                raise AssertionError(f"/proc/{current}/status has no PPid line")

            parent = int(found.group(1))
            comm = (Path("/proc") / str(parent) / "comm").read_text().strip()
            if comm == SandboxBinary.BWRAP.value:
                return parent

            current = parent

        msg = f"no bwrap ancestor of fuse2fs pid {pid}"
        raise AssertionError(msg)

    @staticmethod
    def _workspace(images: Path, template: str) -> WorkspaceSpec:
        return WorkspaceSpec.model_validate(
            {"template": template, "mount": f"{images}/{{user_id}}.ext4:/workspace"}
        )

    def _config(self) -> LocalStorageConfig:
        return LocalStorageConfig(
            kind="image",
            mount_dir=self.MOUNT_DIR,
            op_timeout_sec=60,
            mounting=MountingConfig(
                mount_wait_sec=10.0,
                mount_poll_sec=0.01,
                shutdown_wait_sec=5.0,
                lock_wait_sec=10.0,
                copy_chunk_bytes=1 << 16,
            ),
        )


class PrivateLayer:
    """Слой теста для опытов с точками и ссылками: хост их не видит."""

    SCRIPT: ClassVar[Path] = Path(__file__).resolve().parent / "launcher_layer.py"

    def __init__(self, tmp_path: Path) -> None:
        self._point = tmp_path / "pt"
        self._point.mkdir()
        flap = tmp_path / "flap"
        flap.mkdir()
        self._image = Path(SandboxStand.mkfs_image(flap))
        self._mount = PrivateImageMount(self._image, self._point)
        self._host = os.stat("/proc/self/ns/mnt").st_ino
        self._host_table = MountTable(os.getpid())

    def run(self, command: Sequence[str]) -> dict[str, Any]:
        """Скрипт слоя с аргументами command; итог — его JSON."""
        self.require_unseen()
        argv = self._mount.racing(
            [
                sys.executable,
                str(self.SCRIPT),
                "--host-mntns",
                str(self._host),
                *command,
            ]
        )
        try:
            done = subprocess.run(argv, capture_output=True, text=True, check=False)
        finally:
            self.require_unseen()

        if done.returncode != 0:
            msg = (
                f"test layer {command[0]} exited with {done.returncode}: "
                f"{done.stderr.strip()[-3000:]}"
            )
            raise AssertionError(msg)

        report: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
        return report

    def flap_args(self) -> list[str]:
        return ["--image", str(self._image), "--point", str(self._point)]

    def require_unseen(self) -> None:
        """Точки слоя хост не видит ни до опыта, ни после."""
        seen: list[str] = []
        for point in self._host_table.points():
            if point.startswith(str(self._point.parent)):
                seen.append(point)

        if seen:
            raise AssertionError(f"точки опыта видны хосту: {seen}")


@needs_chain
class TestPrivateLayer:
    """Опыты в собственном слое теста: исчезающая точка и ссылка resolv.conf."""

    SECONDS: ClassVar[float] = 10.0
    WORKERS: ClassVar[int] = 4
    MIN_STARTS: ClassVar[int] = 40
    MIN_CYCLES: ClassVar[int] = 40

    def test_vanishing_host_point_does_not_break_starts(self, tmp_path: Path) -> None:
        """Точка вне вида мигает весь опыт: на корне хоста это 4,9 % сбоев."""
        work = tmp_path / "work"
        work.mkdir()
        layer = PrivateLayer(tmp_path)

        report = layer.run(
            [
                "race",
                *layer.flap_args(),
                "--work",
                str(work),
                "--seconds",
                str(self.SECONDS),
                "--workers",
                str(self.WORKERS),
            ]
        )

        if report["mount_cycles"] < self.MIN_CYCLES:
            raise AssertionError(f"точка почти не мигала: {report}")

        if report["starts"] < self.MIN_STARTS:
            raise AssertionError(f"цепочка почти не стартовала: {report}")

        if report["failures"]:
            raise AssertionError(
                f"{len(report['failures'])} of {report['starts']} starts failed: "
                f"{report['failures'][:3]}"
            )

    def test_resolv_conf_of_systemd_resolved_reaches_the_body(
        self, tmp_path: Path
    ) -> None:
        """/etc/resolv.conf — ссылка в /run: слой видит один этот файл, тело —
        его содержимое на месте /etc/resolv.conf образа."""
        layer = PrivateLayer(tmp_path)

        report = layer.run(["resolved"])

        if report["body"] != ResolvedLayout.CONTENT:
            raise AssertionError(f"resolv.conf тела: {report['body']!r}")

        if ResolvedProbe.BIND.split(":")[0] not in report["sources"]:
            raise AssertionError(f"источник вложенного слоя: {report['sources']}")

        if "/etc/resolv.conf" not in report["points"]:
            raise AssertionError(f"resolv.conf нет в слое: {report['points']}")

        view = LayerView(report["points"], ())
        if view.leaked():
            raise AssertionError(f"слой видит каталоги хоста: {view.leaked()}")
