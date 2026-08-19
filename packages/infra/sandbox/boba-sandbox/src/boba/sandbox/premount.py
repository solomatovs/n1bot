"""Премонтированный корень песочницы: один fuse2fs на образ на всё приложение.

Корневой образ неизменен и одинаков для всех вызовов, поэтому монтируется
однажды при старте приложения; профили переключаются с rootfs_image на
каталог, и запуск инструмента идёт без цепочки лаунчера. Демон fuse2fs живёт
с pdeathsig и умирает вместе с приложением.

Ошибки:
RootfsPremountError — образ не смонтирован, точка занята или недоступна;
    старт приложения с настроенным premount_dir при этом невозможен.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import subprocess
from collections.abc import Mapping
from typing import ClassVar

from boba.sandbox.profile import SandboxProfile
from boba.toolkit.binaries import SandboxBinary, TrustedBinaries
from boba.toolkit.timing import Elapsed
from boba.workspace.launcher import FuseMounter, MountError

__all__ = ["RootfsPremount", "RootfsPremountError"]

logger = logging.getLogger(__name__)


class RootfsPremountError(RuntimeError):
    """Премонтирование корня не состоялось: запуск в этом режиме невозможен."""


class RootfsPremount:
    """Монтирует уникальные rootfs-образы профилей и подменяет профили.

    Активный экземпляр регистрируется на процесс: загрузчик инструментов
    применяет подмену, не зная, включён ли режим. Без активного экземпляра
    apply отдаёт профиль как есть — холодный путь.
    """

    _active: ClassVar[RootfsPremount | None] = None

    def __init__(self, mount_root: str) -> None:
        self._mount_root = mount_root
        self._mounters: list[FuseMounter] = []
        self._mounted: dict[str, str] = {}

    @classmethod
    def activate(cls, mounter: RootfsPremount) -> None:
        cls._active = mounter

    @classmethod
    def reset(cls) -> None:
        cls._active = None

    @classmethod
    def apply(cls, profile: SandboxProfile) -> SandboxProfile:
        """Профиль с премонтированным корнем; без активного режима — как есть."""
        if cls._active is None:
            return profile

        return cls._active._apply(profile)

    def mount_profiles(self, profiles: Mapping[str, SandboxProfile]) -> None:
        """Смонтировать уникальные rootfs_image всех профилей."""
        for name, profile in profiles.items():
            image = profile.rootfs_image
            if not image:
                continue

            if image in self._mounted:
                continue

            self._mount(image, profile, name)

    def shutdown(self) -> None:
        for mounter in self._mounters:
            mounter.shutdown()

        self._mounters.clear()
        self._mounted.clear()

    def mount_point(self, image: str) -> str:
        """Точка монтирования образа: стабильна между запусками приложения."""
        digest = hashlib.sha1(image.encode("utf-8")).hexdigest()[:12]  # noqa: S324

        return os.path.join(self._mount_root, digest)

    def _apply(self, profile: SandboxProfile) -> SandboxProfile:
        image = profile.rootfs_image
        if not image:
            return profile

        mnt = self._mounted.get(image)
        if mnt is None:
            msg = f"rootfs image {image!r} is not premounted"
            raise RootfsPremountError(msg)

        return profile.model_copy(update={"rootfs": mnt, "rootfs_image": ""})

    def _mount(self, image: str, profile: SandboxProfile, name: str) -> None:
        if not os.path.exists(image):
            msg = f"rootfs image {image!r} of profile {name!r} not found"
            raise RootfsPremountError(msg)

        mnt = self.mount_point(image)
        os.makedirs(self._mount_root, exist_ok=True)

        self._release_stale(mnt, profile.binaries)

        mounter = FuseMounter(profile.launcher.to_options(), profile.binaries)

        elapsed = Elapsed()
        try:
            mounter.mount(image, mnt, readonly=True)
        except MountError as exc:
            msg = f"cannot premount {image!r} at {mnt!r}: {exc}"
            raise RootfsPremountError(msg) from exc

        self._mounters.append(mounter)
        self._mounted[image] = mnt

        logger.info(
            "sandbox rootfs premounted: %s -> %s in %dms", image, mnt, elapsed.ms()
        )

    def _release_stale(self, mnt: str, binaries: TrustedBinaries) -> None:
        """Точка от прошлого запуска отмонтируется; живой чужой mount — отказ."""
        if not self._is_mount_point(mnt):
            return

        logger.info("sandbox rootfs: releasing stale mount at %s", mnt)

        if self._umount_syscall(mnt):
            return

        if self._umount_fusermount(mnt, binaries):
            return

        msg = (
            f"stale mount at {mnt!r} cannot be released: no privileges and no "
            f"fusermount; unmount it manually"
        )
        raise RootfsPremountError(msg)

    @staticmethod
    def _is_mount_point(mnt: str) -> bool:
        target = os.path.realpath(mnt)

        with open(FuseMounter.MOUNTINFO) as f:
            for line in f:
                if line.split()[4] == target:
                    return True

        return False

    @staticmethod
    def _umount_syscall(mnt: str) -> bool:
        libc = ctypes.CDLL(None, use_errno=True)

        if libc.umount(mnt.encode("utf-8")) == 0:
            return True

        code = ctypes.get_errno()
        if code in (errno.EPERM, errno.EACCES):
            return False

        msg = f"umount({mnt!r}): {os.strerror(code)}"
        raise RootfsPremountError(msg)

    @staticmethod
    def _umount_fusermount(mnt: str, binaries: TrustedBinaries) -> bool:
        """suid-обвязка libfuse: снимает fuse-точку без CAP_SYS_ADMIN."""
        candidates = (SandboxBinary.FUSERMOUNT3, SandboxBinary.FUSERMOUNT)

        for binary in candidates:
            if not binaries.has(binary):
                continue

            path = binaries.resolve(binary)
            done = subprocess.run(  # noqa: S603
                [path, "-u", mnt], capture_output=True, text=True, check=False
            )
            if done.returncode == 0:
                return True

            logger.warning(
                "sandbox rootfs: %s -u %s failed: %s",
                binary.value,
                mnt,
                done.stderr.strip(),
            )

        return False
