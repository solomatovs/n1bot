"""Образы корней плагинов: точки монтирования данных обязаны быть в образе.

Рантайм монтирует веса эмбеддера и модели OCR образами данных, которые
объявил манифест плагина, а корень
плагина read-only: нет пустого каталога в образе — bwrap не создаст его и
секция не поднимется. Сборщик такие каталоги создаёт, тест проверяет результат.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

import pytest

from boba.sandbox.program import PluginEntry
from boba.stand.zygote import RUNTIME, PrivateImageMount, SandboxStand
from boba.toolkit.manifest import ToolPlugin

FUSE2FS = SandboxStand.FUSE2FS


class DataPlugins:
    """Плагины, объявившие образы данных: точки монтирования по манифесту."""

    def __init__(self) -> None:
        self._entry = PluginEntry()

    def ids(self) -> list[str]:
        ids: list[str] = []
        for entry in entry_points(group=ToolPlugin.GROUP):
            if self.targets(entry.name):
                ids.append(entry.name)

        return sorted(ids)

    def targets(self, plugin_id: str) -> list[str]:
        targets: list[str] = []
        for image in self._entry.program(plugin_id).plugin.data:
            targets.append(image.target)

        return targets


PLUGINS = DataPlugins()
IDS = PLUGINS.ids()

needs_images = pytest.mark.failif(
    not FUSE2FS.exists()
    or not all(RUNTIME.plugin_rootfs(plugin_id).exists() for plugin_id in IDS),
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
    @pytest.mark.parametrize("plugin_id", IDS)
    def test_declared_data_paths_exist_in_the_image(
        self, plugin_id: str, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        image = ImageDirs(
            RUNTIME.plugin_rootfs(plugin_id), tmp_path_factory.mktemp("rootfs")
        )

        missing = image.missing(PLUGINS.targets(plugin_id))

        assert missing == [], f"{plugin_id}: в образе нет точек монтирования: {missing}"
