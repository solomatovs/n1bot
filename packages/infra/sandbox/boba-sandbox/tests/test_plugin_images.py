"""Образы корней плагинов: точки монтирования данных обязаны быть в образе.

Рантайм монтирует веса эмбеддера и модели OCR образами данных, а корень
плагина read-only: нет пустого каталога в образе — bwrap не создаст его и
секция не поднимется. Сборщик такие каталоги создаёт, тест проверяет результат.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from boba.stand.sandbox import SandboxLayout
from boba.stand.zygote import RUNTIME, PrivateImageMount, SandboxStand

FUSE2FS = SandboxStand.FUSE2FS

PACKAGES = sorted(SandboxLayout.DATA_IMAGES)

needs_images = pytest.mark.failif(
    not FUSE2FS.exists()
    or not all(RUNTIME.plugin_rootfs(package).exists() for package in PACKAGES),
    reason=(
        "нет артефактов песочницы (собрать: make -C build sandbox plugin-rootfs-all)"
    ),
)


class ImageDirs:
    """Каталоги, которых нет в образе корня плагина: образ смонтирован в
    собственном пространстве имён (PrivateImageMount), а не в дереве хоста."""

    MISSING: ClassVar[str] = (
        'root=$1; shift; for dir in "$@"; do [ -d "$root$dir" ] || echo "$dir"; done'
    )

    def __init__(self, image: Path, point: Path) -> None:
        self._mount = PrivateImageMount(image, point)
        self._point = point

    def missing(self, guests: Sequence[str]) -> list[str]:
        command = ["/bin/sh", "-c", self.MISSING, "sh", str(self._point), *guests]
        done = subprocess.run(
            self._mount.argv(command), capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            msg = (
                f"checking directories of the image mounted at {self._point} "
                f"exited with {done.returncode}: {done.stderr.strip()}"
            )
            raise AssertionError(msg)

        return done.stdout.split()


@needs_images
class TestPluginDataMountpoints:
    @pytest.mark.parametrize("package", PACKAGES)
    def test_declared_data_paths_exist_in_the_image(
        self, package: str, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        image = ImageDirs(
            RUNTIME.plugin_rootfs(package), tmp_path_factory.mktemp("rootfs")
        )

        guests: list[str] = []
        for _, guest in SandboxLayout.DATA_IMAGES[package]:
            guests.append(guest)

        missing = image.missing(guests)

        assert missing == [], f"{package}: в образе нет точек монтирования: {missing}"
