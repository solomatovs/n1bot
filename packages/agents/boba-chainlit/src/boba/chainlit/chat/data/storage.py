"""Реализации chainlit BaseStorageClient: локальный диск и ext4-образ треда."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, ClassVar

import aiofiles
import aiofiles.os

from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.infra.config import LocalStorageConfig
from boba.toolkit.workspace import build_chain_argv, render_image_path, require_fuse
from boba.toolkit.workspace.launcher import (
    EXIT_MOUNT_ERROR,
    EXIT_NOT_FOUND,
    LAUNCHER_ERROR_PREFIX,
    LAUNCHER_LOG_PREFIX,
)
from boba.toolkit.workspace.options import ResourceLimits
from chainlit.data.storage_clients.base import BaseStorageClient

__all__ = ["ImageStorageClient", "LocalStorageClient"]

logger = logging.getLogger(__name__)


class LocalStorageClient(BaseStorageClient):
    """Хранит файлы вложений на локальном диске под files_dir."""

    PREFIX_VAR: ClassVar[str] = "{public_prefix}"
    KEY_VAR: ClassVar[str] = "{object_key}"
    URL_TEMPLATE: ClassVar[str] = PREFIX_VAR + "/" + KEY_VAR
    """Хранимый url — шаблон: сам ключ лежит в object_key и не дублируется."""

    def __init__(self, config: LocalStorageConfig) -> None:
        self._config = config

    @classmethod
    def from_config(cls, config: LocalStorageConfig) -> LocalStorageClient:
        if config.kind == "image":
            return ImageStorageClient(config)
        return LocalStorageClient(config)

    def _resolve(self, object_key: str) -> Path:
        base_dir = Path(self._config.files_dir).resolve()
        path = (base_dir / object_key).resolve()

        if not path.is_relative_to(base_dir):
            raise ValueError(f"object_key outside files_dir: {object_key!r}")

        return path

    def render_url(self, url: str, object_key: str) -> str:
        return url.replace(
            self.PREFIX_VAR, self._config.public_prefix.rstrip("/")
        ).replace(self.KEY_VAR, object_key)

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
                "url": self.URL_TEMPLATE,
            }

        await aiofiles.os.makedirs(path.parent, exist_ok=True)

        payload = data.encode() if isinstance(data, str) else data

        async with aiofiles.open(path, "wb") as f:
            await f.write(payload)

        return {"object_key": object_key, "url": self.URL_TEMPLATE}

    async def read_file(self, object_key: str) -> bytes:
        path = self._resolve(object_key)
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def get_read_url(self, object_key: str) -> str:
        return self.render_url(self.URL_TEMPLATE, object_key)

    async def delete_file(self, object_key: str) -> bool:
        path = self._resolve(object_key)
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            return False
        return True

    async def close(self) -> None:
        pass


class ImageStorageClient(LocalStorageClient):
    """Хранит вложения внутри per-thread ext4-образа: fuse2fs на одну операцию."""

    async def upload_file(
        self,
        object_key: str,
        data: bytes | str,
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> dict[str, Any]:
        image, rel = self._image_and_rel(object_key)
        if not overwrite and await self._exists(image, rel):
            return {"object_key": object_key, "url": self.URL_TEMPLATE}

        payload = data.encode() if isinstance(data, str) else data
        rc, _, err = await self._op(image, ["write", rel], stdin_data=payload)
        self._check(rc, err)
        return {"object_key": object_key, "url": self.URL_TEMPLATE}

    async def read_file(self, object_key: str) -> bytes:
        image, rel = self._image_and_rel(object_key)
        rc, out, err = await self._op(image, ["read", rel])
        if rc == EXIT_NOT_FOUND:
            raise FileNotFoundError(object_key)
        self._check(rc, err)
        return out

    async def delete_file(self, object_key: str) -> bool:
        image, rel = self._image_and_rel(object_key)
        rc, _, err = await self._op(image, ["delete", rel])
        if rc == EXIT_NOT_FOUND:
            return False
        self._check(rc, err)
        return True

    def _image_and_rel(self, object_key: str) -> tuple[str, str]:
        key = ObjectKey.parse(object_key)
        image = render_image_path(
            self._config.image_path,
            {"user_id": key.user_id, "thread_id": key.thread_id},
        )
        # образ общий на пользователя: thread_id остаётся частью пути внутри
        return image, key.in_thread()

    async def _exists(self, image: str, rel: str) -> bool:
        rc, _, err = await self._op(image, ["read", rel])
        if rc == EXIT_NOT_FOUND:
            return False
        self._check(rc, err)
        return True

    async def _op(
        self,
        image: str,
        op: list[str],
        stdin_data: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        require_fuse()
        await aiofiles.os.makedirs(os.path.dirname(image), exist_ok=True)
        argv = build_chain_argv(
            images=[(image, image + ".mnt")],
            template=self._config.image_template,
            op=op,
            python_bin=sys.executable,
            options=self._config.launcher.to_options(),
            limits=ResourceLimits(),
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        started = time.monotonic()
        logger.info("storage: operation %s in image %s", op, image)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=self._config.op_timeout_sec,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            msg = (
                f"storage: image operation {op[0]!r} did not finish "
                f"within {self._config.op_timeout_sec}s"
            )
            raise RuntimeError(msg) from None
        err = self._drain_launcher_log(err)
        code = proc.returncode
        if code is None:
            code = 0
        logger.info(
            "storage: %s finished rc=%s in %sms (%s bytes)",
            op[0],
            code,
            int((time.monotonic() - started) * 1000),
            len(out),
        )
        return code, out, err

    @staticmethod
    def _drain_launcher_log(err: bytes) -> bytes:
        """Ход монтирования — в лог; наверх идёт только то, что не от лаунчера."""
        text = err.decode("utf-8", errors="replace")
        if LAUNCHER_LOG_PREFIX not in text:
            return err
        kept: list[str] = []
        for line in text.splitlines():
            if line.startswith(LAUNCHER_LOG_PREFIX):
                logger.info("storage: %s", line[len(LAUNCHER_LOG_PREFIX) :])
            else:
                kept.append(line)
        return "\n".join(kept).encode("utf-8")

    @staticmethod
    def _check(rc: int, err: bytes) -> None:
        if rc == 0:
            return
        detail = err.decode("utf-8", errors="replace").strip()
        if rc == EXIT_MOUNT_ERROR and LAUNCHER_ERROR_PREFIX in detail:
            msg = f"storage: image not mounted: {detail}"
            raise RuntimeError(msg)
        msg = f"storage: image operation exited with code {rc}: {detail}"
        raise RuntimeError(msg)
