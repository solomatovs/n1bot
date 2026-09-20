"""Образы корней плагинов: точки монтирования данных обязаны быть в образе.

Рантайм монтирует веса эмбеддера и tessdata биндами, а корень плагина
read-only: нет пустого каталога в образе — bwrap не создаст его и секция не
поднимется. Сборщик такие каталоги создаёт, тест проверяет результат.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest

from boba.stand.sandbox import SandboxLayout, plugin_rootfs

REPO = Path(__file__).resolve().parents[5]
FUSE2FS = REPO / "build" / "chainlit" / "src" / "sandbox" / "third" / "bin" / "fuse2fs"

PACKAGES = sorted(SandboxLayout.DATA_BINDS)

needs_images = pytest.mark.skipif(
    not FUSE2FS.exists()
    or not all(plugin_rootfs(package).exists() for package in PACKAGES),
    reason=(
        "нет артефактов песочницы "
        "(собрать: make -C build/chainlit sandbox plugin-rootfs-all)"
    ),
)


class MountedImage:
    """Образ корня плагина, смонтированный fuse2fs только на чтение."""

    UMOUNT: ClassVar[str] = "fusermount3"

    def __init__(self, image: Path, point: Path) -> None:
        self._image = image
        self._point = point

    @contextmanager
    def opened(self) -> Generator[Path, None, None]:
        subprocess.run(
            [str(FUSE2FS), "-o", "ro", str(self._image), str(self._point)],
            check=True,
            capture_output=True,
        )
        try:
            yield self._point
        finally:
            self._umount()

    def _umount(self) -> None:
        fusermount = shutil.which(self.UMOUNT)
        if fusermount is None:
            msg = f"{self.UMOUNT} недоступен: {self._point} осталась смонтированной"
            raise AssertionError(msg)

        subprocess.run([fusermount, "-u", str(self._point)], check=True)


@needs_images
class TestPluginDataMountpoints:
    @pytest.mark.parametrize("package", PACKAGES)
    def test_declared_data_paths_exist_in_the_image(
        self, package: str, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        image = MountedImage(plugin_rootfs(package), tmp_path_factory.mktemp("rootfs"))

        missing: list[str] = []
        with image.opened() as root:
            for _, guest in SandboxLayout.DATA_BINDS[package]:
                if (root / guest.lstrip("/")).is_dir():
                    continue

                missing.append(guest)

        assert missing == [], f"{package}: в образе нет точек монтирования: {missing}"
