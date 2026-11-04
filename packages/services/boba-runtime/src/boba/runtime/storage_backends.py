"""Готовые реализации хранилища: каталог файлов и образ ext4.

Ошибки:
StorageError — конфигурация или ресурсы выбранного хранилища недоступны.
"""

from boba.core.components import ComponentFactory, Components
from boba.runtime.config import InstanceData, PathsConfig, ProgramRoots
from boba.runtime.storage import (
    ImageStorageClient,
    ImageStorageConfig,
    LocalStorageClient,
    LocalStorageConfig,
    StorageClient,
    StorageConfig,
)


class StandardStorageBackends:
    """Фабрики локального и ext4-хранилища, которые явно подключает приложение."""

    def __init__(self) -> None:
        self._programs = ProgramRoots()
        self.registry: Components[StorageConfig, PathsConfig, StorageClient] = (
            Components(
                StorageConfig,
                "kind",
                (
                    ComponentFactory(LocalStorageConfig, self.local),
                    ComponentFactory(ImageStorageConfig, self.image),
                ),
            )
        )

    def local(self, config: LocalStorageConfig, paths: PathsConfig) -> StorageClient:
        return LocalStorageClient(config)

    def image(self, config: ImageStorageConfig, paths: PathsConfig) -> StorageClient:
        program = self._programs.locate()
        images = paths.data(InstanceData.WORKSPACE)
        return ImageStorageClient(config, program.workspace(images), program)
