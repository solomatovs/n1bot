"""Посев файлов в хранилище workspace для тестов."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import ClassVar

from boba.runtime.storage import StorageClient

__all__ = ["StorageSeed"]


class StorageSeed:
    """Кладёт в хранилище готовые байты теста.

    Хранилище пишет только потоком: метода, принимающего содержимое целиком,
    у него нет. Тесту удобно задать файл литералом, и этот класс подаёт его
    хранилищу одним чанком. Создаётся тестом по месту.
    """

    ENCODING: ClassVar[str] = "utf-8"

    async def put(
        self, storage: StorageClient, object_key: str, data: bytes | str
    ) -> None:
        await storage.upload_stream(object_key, self._chunks(data))

    async def _chunks(self, data: bytes | str) -> AsyncGenerator[bytes, None]:
        if isinstance(data, str):
            data = data.encode(self.ENCODING)

        yield data
