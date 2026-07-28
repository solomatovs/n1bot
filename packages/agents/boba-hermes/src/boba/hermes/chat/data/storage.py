"""
LocalStorageClient

реализация chainlit BaseStorageClient поверх локального диска
"""

from pathlib import Path
from typing import Any

import aiofiles
import aiofiles.os
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.logger import logger

from boba.hermes.infra.config import LocalStorageConfig

__all__ = ["LocalStorageClient"]


class LocalStorageClient(BaseStorageClient):
    """Хранит файлы вложений на локальном диске под files_dir."""

    def __init__(self, config: LocalStorageConfig) -> None:
        self._config = config

    def _resolve(self, object_key: str) -> Path:
        """Путь файла внутри files_dir"""
        base_dir = Path(self._config.files_dir)
        path = (base_dir / object_key).resolve()

        if not path.is_relative_to(base_dir):
            raise ValueError(f"object_key outside files_dir: {object_key!r}")

        return path

    def _url(self, object_key: str) -> str:
        return f"{object_key}"

    async def upload_file(
        self,
        object_key: str,
        data: bytes | str,
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> dict[str, Any]:
        path = self._resolve(object_key)
        if path.exists() and not overwrite:
            return {
                "object_key": object_key,
                "url": self._url(object_key),
            }

        await aiofiles.os.makedirs(path.parent, exist_ok=True)

        payload = data.encode() if isinstance(data, str) else data

        async with aiofiles.open(path, "wb") as f:
            await f.write(payload)

        return {"object_key": object_key, "url": self._url(object_key)}

    async def get_read_url(self, object_key: str) -> str:
        return self._url(object_key)

    async def delete_file(self, object_key: str) -> bool:
        try:
            path = self._resolve(object_key)
        except ValueError:
            return False
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            return False
        except OSError as e:
            logger.warning(f"LocalStorageClient: failed to delete {object_key}: {e}")
            return False
        return True

    async def close(self) -> None:
        # диск не держит ресурсов — закрывать нечего
        pass
