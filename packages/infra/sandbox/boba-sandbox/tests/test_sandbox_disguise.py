"""Маскировка хоста: тело инструмента не узнаёт машину снаружи и контейнер.

Проверяется на живой цепочке запуска (bwrap + зигота + исполнитель) кодом
репозитория: профиль стенда накладывает src поверх собранного образа, поэтому
тест видит текущий HostMask, а не запечённый в rootfs плагина.

Остаются принципиальные утечки, которые закрываются только новыми правами и
потому вынесены отдельными тестами с причиной: версия ядра из uname(2) и
признак fuse в таблице монтирования.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import os
import shutil
from typing import ClassVar

import pytest

from boba.sandbox.guest import HostMask
from boba.stand.shell import ShellRun
from boba.stand.zygote import SandboxStand, ZygoteStand

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
        "echo mountinfo_fuse=$(grep -c fuse.ext4 /proc/self/mountinfo); "
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


class TestKnownLimitations:
    """Утечки, которые без новых прав не закрыть; тест фиксирует их явно.

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

    def test_mountinfo_still_reveals_fuse(self, probe: Probe) -> None:
        """Источник корня в mountinfo — fuse.ext4 с путём образа на хосте.

        Нейтрализуется только перестроением дерева монтирования (pivot_root в
        исполнителе) — предложение в итоге; fsname fuse2fs перекрыть не даёт.
        """
        if probe.value("mountinfo_fuse") == "0":
            raise AssertionError(
                "fuse исчез из mountinfo — ограничение снято, обновить итог задачи"
            )
