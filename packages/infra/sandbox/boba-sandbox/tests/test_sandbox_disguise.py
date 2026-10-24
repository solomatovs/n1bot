"""Маскировка хоста: тело инструмента не узнаёт машину снаружи и контейнер.

Проверяется на живой цепочке запуска (bwrap + зигота + исполнитель) кодом
репозитория: профиль стенда накладывает src поверх собранного образа, поэтому
тест видит текущий HostMask, а не запечённый в rootfs плагина.

Остаются принципиальные утечки, которые закрываются только новыми правами или
иной архитектурой и потому вынесены отдельными тестами с причиной: версия ядра
из uname(2), тип fuse у корня и сами следы маскировки (bind поверх /proc).

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from boba.sandbox.guest import HostMask
from boba.stand.shell import ShellRun
from boba.stand.zygote import RUNTIME, SandboxStand, ZygoteStand

needs_bwrap = pytest.mark.failif(
    shutil.which("bwrap") is None,
    reason="нет bubblewrap",
)
needs_userns = pytest.mark.failif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

pytestmark = [needs_bwrap, needs_userns]

MEM_BYTES = 2 * 1024 * 1024 * 1024


class Probe:
    """Снимок признаков хоста изнутри песочницы одной shell-командой."""

    SCRIPT: ClassVar[str] = (
        "echo comm=$(cat /proc/1/comm); "
        "echo node=$(uname -n); "
        "echo release=$(uname -r); "
        "echo memtotal=$(awk '/MemTotal/{print $2}' /proc/meminfo); "
        "echo cpumodel=$(awk -F': ' '/model name/{print $2; exit}' /proc/cpuinfo); "
        "echo loadavg=$(cat /proc/loadavg); "
        "echo modules=$(wc -l < /proc/modules); "
        "echo version=$(cat /proc/version); "
        "echo bootid=$(cat /proc/sys/kernel/random/boot_id)"
    )

    def __init__(self, stand: ZygoteStand) -> None:
        profile = SandboxStand.profile(process_memory_bytes=MEM_BYTES)
        caller = stand.caller("disguise", profile)
        outcome = ShellRun.call_text(caller, self.SCRIPT)
        if outcome.exit_code != 0:
            msg = f"probe failed rc={outcome.exit_code}: {outcome.stderr!r}"
            raise AssertionError(msg)

        self._fields = self._parse(outcome.stdout)

    @staticmethod
    def _parse(stdout: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key] = value

        return fields

    def value(self, key: str) -> str:
        if key not in self._fields:
            msg = f"probe has no field {key!r}: {self._fields}"
            raise AssertionError(msg)

        return self._fields[key]


@pytest.fixture
def probe(zygote_stand: ZygoteStand) -> Probe:
    made = Probe(zygote_stand)
    zygote_stand.stop()
    return made


class MountProbe:
    """Таблица монтирования и содержимое конфига изнутри секции с workspace.

    Профиль несёт образ workspace (fuse rw), образ данных (fuse ro, как веса
    моделей) и бинд хостового resolv.conf — так один вызов покрывает источник
    fuse у корня и у образов, каталог образов и нейтрализацию хостового бинда
    конфига.
    """

    FAKE_DNS: ClassVar[str] = "nameserver 10.1.2.3"
    WEIGHTS: ClassVar[str] = "weights-of-the-model"
    DATA_TARGET: ClassVar[str] = "/media"
    """Точка образа данных: пустой каталог, который есть в корне shell-плагина."""
    MOUNTS: ClassVar[str] = "@@mounts@@"
    RESOLV: ClassVar[str] = "@@resolv@@"
    DATA: ClassVar[str] = "@@data@@"
    STATUS: ClassVar[str] = "@@status@@"
    SCRIPT: ClassVar[str] = (
        "cat /proc/self/mountinfo; "
        f"echo {MOUNTS}; cat /proc/mounts; "
        f"echo {RESOLV}; cat /etc/resolv.conf; "
        f"echo {DATA}; cat {DATA_TARGET}/weights.txt; "
        f"echo {STATUS}; cat /proc/self/status"
    )
    STAND_CODE: ClassVar[str] = "/usr/src"
    """Бинд кода репозитория, который кладёт сам стенд: в бою его нет."""

    def __init__(self, stand: ZygoteStand, tmp_path: Path) -> None:
        fake = tmp_path / "resolv.conf"
        fake.write_text(f"{self.FAKE_DNS}\n")
        self._fake = str(fake)
        self._image = self._data_image(tmp_path)

        # код репозитория (/usr/src) остаётся: иначе зигота взяла бы guest из
        # запечённого образа, а не из текущих исходников
        profile = SandboxStand.image_profile(
            tmp_path,
            process_memory_bytes=MEM_BYTES,
            ro=(*SandboxStand.image_ro_binds(), f"{fake}:/etc/resolv.conf"),
            images=(f"{self._image}:{self.DATA_TARGET}",),
        )
        caller = stand.caller(
            "mnt", profile, path_vars=lambda: {"user_id": "3", "scope_id": "x"}
        )
        outcome = ShellRun.call_text(caller, self.SCRIPT)
        if outcome.exit_code != 0:
            msg = f"mount probe failed rc={outcome.exit_code}: {outcome.stderr!r}"
            raise AssertionError(msg)

        self._mountinfo, _, rest = outcome.stdout.partition(f"{self.MOUNTS}\n")
        self._mounts, _, rest = rest.partition(f"{self.RESOLV}\n")
        self._resolv, _, rest = rest.partition(f"{self.DATA}\n")
        self._data, _, self._status = rest.partition(f"{self.STATUS}\n")

    def _data_image(self, tmp_path: Path) -> str:
        """Ext4-образ данных с одним файлом весов: mke2fs кладёт дерево сам."""
        mkfs = shutil.which("mkfs.ext4")
        if mkfs is None:
            msg = "mount probe: mkfs.ext4 is not found on PATH for the data image"
            raise AssertionError(msg)

        tree = tmp_path / "data-tree"
        tree.mkdir()
        (tree / "weights.txt").write_text(f"{self.WEIGHTS}\n")

        image = tmp_path / "data.ext4"
        subprocess.run(
            [mkfs, "-q", "-F", "-t", "ext4", "-d", str(tree), str(image), "4m"],
            check=True,
            capture_output=True,
        )
        return str(image)

    @property
    def mountinfo(self) -> str:
        return self._mountinfo

    @property
    def mounts(self) -> str:
        return self._mounts

    @property
    def resolv(self) -> str:
        return self._resolv

    @property
    def fake_host_path(self) -> str:
        return self._fake

    @property
    def data(self) -> str:
        return self._data

    @property
    def status(self) -> str:
        return self._status

    def table_lines(self) -> list[str]:
        """Строки mountinfo и mounts без бинда кода стенда."""
        lines: list[str] = []
        for line in (self._mountinfo + self._mounts).splitlines():
            fields = line.split()
            if self.STAND_CODE in fields[1:5]:
                continue

            lines.append(line)

        return lines

    @property
    def data_image_path(self) -> str:
        return self._image

    def fuse_sources(self) -> list[str]:
        """Источник (поле после типа) каждой строки fuse mountinfo."""
        sources: list[str] = []
        for line in self._mountinfo.splitlines():
            _, sep, tail = line.partition(" - ")
            if not sep:
                continue

            fields = tail.split()
            if len(fields) >= 2 and fields[0].startswith("fuse"):
                sources.append(fields[1])

        return sources


@pytest.fixture
def mount_probe(zygote_stand: ZygoteStand, tmp_path: Path) -> MountProbe:
    made = MountProbe(zygote_stand, tmp_path)
    zygote_stand.stop()
    return made


class TestHostIsMasked:
    """Признаки хоста подменены нейтральными значениями HostMask."""

    def test_pid1_looks_like_init(self, probe: Probe) -> None:
        if probe.value("comm") != HostMask.COMM:
            raise AssertionError(f"pid 1 comm: {probe.value('comm')!r}")

    def test_hostname_is_neutral(self, probe: Probe) -> None:
        if probe.value("node") != HostMask.HOSTNAME:
            raise AssertionError(f"uname -n: {probe.value('node')!r}")

        if os.uname().nodename in probe.value("node"):
            raise AssertionError("тело видит имя хоста машины")

    def test_cpu_model_is_neutral(self, probe: Probe) -> None:
        if probe.value("cpumodel") != HostMask.CPU_MODEL:
            raise AssertionError(f"model name: {probe.value('cpumodel')!r}")

        host_model = self._host_cpu_model()
        if host_model and host_model in probe.value("cpumodel"):
            raise AssertionError("тело видит модель процессора хоста")

    def test_memtotal_matches_the_limit(self, probe: Probe) -> None:
        if probe.value("memtotal") != str(MEM_BYTES // 1024):
            raise AssertionError(f"MemTotal: {probe.value('memtotal')!r}")

    def test_loadavg_is_neutral(self, probe: Probe) -> None:
        if probe.value("loadavg") != "0.00 0.00 0.00 1/1 1":
            raise AssertionError(f"loadavg: {probe.value('loadavg')!r}")

    def test_modules_are_hidden(self, probe: Probe) -> None:
        if probe.value("modules") != "0":
            raise AssertionError(f"модули ядра видны: {probe.value('modules')!r}")

    def test_version_drops_build_fingerprint(self, probe: Probe) -> None:
        version = probe.value("version")
        if "builder@localhost" not in version:
            raise AssertionError(f"версия не замаскирована: {version!r}")

        host_version = self._host_proc_version()
        if "archlinux" in version or host_version in version:
            raise AssertionError(f"версия выдаёт сборку хоста: {version!r}")

    def test_bootid_is_not_the_host_bootid(self, probe: Probe) -> None:
        if probe.value("bootid") == self._host_boot_id():
            raise AssertionError("тело видит boot_id хоста")

    @staticmethod
    def _host_cpu_model() -> str:
        with open("/proc/cpuinfo") as handle:
            for line in handle:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()

        return ""

    @staticmethod
    def _host_proc_version() -> str:
        with open("/proc/version") as handle:
            return handle.read().strip()

    @staticmethod
    def _host_boot_id() -> str:
        with open("/proc/sys/kernel/random/boot_id") as handle:
            return handle.read().strip()


class HostOwner:
    """Признаки пользователя хоста: uid= и gid= в опциях монтирования, число
    uid в строках Uid/Gid статуса и имя пользователя отдельным словом."""

    def markers(self) -> list[re.Pattern[str]]:
        uid = os.getuid()
        gid = os.getgid()
        user = pwd.getpwuid(uid).pw_name
        return [
            re.compile(rf"\buid={uid}\b"),
            re.compile(rf"\bgid={gid}\b"),
            re.compile(rf"^(Uid|Gid):.*\b({uid}|{gid})\b"),
            re.compile(rf"(?<![\w.-]){re.escape(user)}(?![\w.-])"),
        ]


class TestMountinfoIsClean:
    """В таблице монтирования нет ничего хостового: ни путей образов, ни
    host-путей у биндов конфигов."""

    def test_fuse_sources_are_neutral(self, mount_probe: MountProbe) -> None:
        """Источник корня и образа workspace — нейтральное имя, не путь."""
        sources = mount_probe.fuse_sources()
        if not sources:
            raise AssertionError("fuse-монтирований в таблице нет — проверка слепа")

        for source in sources:
            if "/" in source:
                raise AssertionError(f"fuse выдаёт путь образа: {source!r}")

    def test_no_images_directory(self, mount_probe: MountProbe) -> None:
        if "/mnt/images" in mount_probe.mountinfo + mount_probe.mounts:
            raise AssertionError("каталог образов виден в таблице монтирования")

    def test_no_rootfs_image_path(self, mount_probe: MountProbe) -> None:
        rootfs = str(RUNTIME.shell_rootfs)
        if rootfs in mount_probe.mountinfo or rootfs in mount_probe.mounts:
            raise AssertionError("путь образа rootfs виден в таблице монтирования")

    def test_config_bind_is_neutralized(self, mount_probe: MountProbe) -> None:
        """Хостовый бинд resolv.conf заменён копией: путь на хосте исчез,
        содержимое сохранено."""
        if mount_probe.fake_host_path in mount_probe.mountinfo:
            raise AssertionError("host-путь бинда resolv.conf виден в mountinfo")

        if MountProbe.FAKE_DNS not in mount_probe.resolv:
            msg = f"содержимое resolv.conf потеряно: {mount_probe.resolv!r}"
            raise AssertionError(msg)

    def test_data_image_is_mounted_neutrally(self, mount_probe: MountProbe) -> None:
        """Образ данных (как веса моделей) читается телом, а путь образа на
        хосте в таблице монтирования не виден."""
        if MountProbe.WEIGHTS not in mount_probe.data:
            msg = f"файл образа данных не читается: {mount_probe.data!r}"
            raise AssertionError(msg)

        table = mount_probe.mountinfo + mount_probe.mounts
        if mount_probe.data_image_path in table:
            raise AssertionError("путь образа данных виден в таблице монтирования")

    def test_no_host_owner(self, mount_probe: MountProbe) -> None:
        """Ни uid, ни gid, ни имя пользователя хоста: tmpfs из userns печатала
        их в опциях, поэтому /tmp, /dev и подмены тела стоят на ramfs."""
        markers = HostOwner().markers()

        exposed: list[str] = []
        for line in [*mount_probe.table_lines(), *mount_probe.status.splitlines()]:
            for marker in markers:
                if marker.search(line):
                    exposed.append(line)

        if exposed:
            listed = "\n".join(exposed)
            raise AssertionError(f"тело видит владельца на хосте:\n{listed}")

    def test_no_host_nodename(self, mount_probe: MountProbe) -> None:
        if os.uname().nodename in mount_probe.mountinfo:
            raise AssertionError("имя хоста машины видно в таблице монтирования")


class TestKnownLimitations:
    """Утечки, которые без новых прав или иной архитектуры не закрыть; тест
    фиксирует их явно.

    Если такой тест упадёт — ограничение снято, и запись о нём в итоге задачи
    пора обновить, а не промолчать.
    """

    def test_kernel_release_leaks_via_uname(self, probe: Probe) -> None:
        """uname(2) отдаёт release ядра системным вызовом: /proc его не меняет.

        Подменить можно только seccomp user notify или ptrace — оба требуют
        прав сверх профиля песочницы и вынесены предложением.
        """
        if probe.value("release") != os.uname().release:
            raise AssertionError(
                "uname -r больше не совпадает с ядром хоста — ограничение снято, "
                "обновить итог задачи"
            )

    def test_root_fstype_still_shows_fuse(self, mount_probe: MountProbe) -> None:
        """Корень — fuse.ext4: тип файловой системы скрыть нельзя, корень
        действительно обслуживает fuse2fs. Источник уже нейтрален (отдельная
        проверка)."""
        if "fuse.ext4" not in mount_probe.mountinfo:
            raise AssertionError(
                "fuse.ext4 исчез из mountinfo — архитектура корня изменилась, "
                "обновить итог задачи"
            )

    def test_proc_masks_show_as_overmounts(self, mount_probe: MountProbe) -> None:
        """Подмена файлов /proc — bind поверх, он виден строкой tmpfs в
        mountinfo. Убрать след можно лишь fuse-видом /proc (как LXCFS) —
        отдельный демон, вынесено предложением."""
        if "/proc/cpuinfo" not in mount_probe.mountinfo:
            raise AssertionError(
                "bind поверх /proc/cpuinfo исчез — способ маскировки изменился, "
                "обновить итог задачи"
            )
