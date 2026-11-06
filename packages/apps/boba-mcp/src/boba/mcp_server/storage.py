"""Готовые реализации хранилища: каталог файлов и образ ext4.

Ошибки:
StorageError — конфигурация или ресурсы выбранного хранилища недоступны.
"""

from pathlib import Path

from boba.core.components import ComponentFactory, Components
from boba.sandbox.paths import ProgramRoots, WorkspaceData
from boba.sandbox.storage import ImageStorageClient, ImageStorageConfig
from boba.storage.client import (
    LocalStorageClient,
    LocalStorageConfig,
    StorageClient,
    StorageConfig,
)


class StandardStorageBackends:
    """Фабрики локального и ext4-хранилища, которые явно подключает приложение."""

    def __init__(self) -> None:
        self._programs = ProgramRoots()
        self.registry: Components[StorageConfig, str, StorageClient] = Components(
            StorageConfig,
            "kind",
            (
                ComponentFactory(LocalStorageConfig, self.local),
                ComponentFactory(ImageStorageConfig, self.image),
            ),
        )

    def local(self, config: LocalStorageConfig, data_dir: str) -> StorageClient:
        return LocalStorageClient(config)

    def image(self, config: ImageStorageConfig, data_dir: str) -> StorageClient:
        program = self._programs.locate()
        images = Path(data_dir) / WorkspaceData.WORKSPACE
        return ImageStorageClient(config, program.workspace(images), program)
