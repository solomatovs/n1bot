"""Реализации chainlit BaseStorageClient: локальный диск и ext4-образ треда."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import aiofiles
import aiofiles.os

from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.infra.config import LocalStorageConfig
from boba.workspace.launcher import (
    EXIT_MOUNT_ERROR,
    EXIT_NO_SPACE,
    EXIT_NOT_FOUND,
    LAUNCHER_ERROR_PREFIX,
    LAUNCHER_LOG_PREFIX,
    ResourceLimits,
    build_chain_argv,
    render_image_path,
    require_fuse,
)
from chainlit.data.storage_clients.base import BaseStorageClient

__all__ = [
    "ImageStorageClient",
    "LocalStorageClient",
    "StorageError",
    "StorageFullError",
]

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Хранилище не выполнило операцию."""


class StorageFullError(StorageError):
    """В образе пользователя не осталось места под файл."""


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

        if isinstance(data, str):
            payload = data.encode()
        else:
            payload = data

        async with aiofiles.open(path, "wb") as f:
            await f.write(payload)

        return {"object_key": object_key, "url": self.URL_TEMPLATE}

    async def upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Кладёт файл чанками: содержимое целиком в память не поднимается."""
        path = self._resolve(object_key)
        await aiofiles.os.makedirs(path.parent, exist_ok=True)

        async with aiofiles.open(path, "wb") as f:
            async for chunk in source:
                await f.write(chunk)

        return {"object_key": object_key, "url": self.URL_TEMPLATE}

    async def read_file(self, object_key: str) -> bytes:
        path = self._resolve(object_key)
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def read_stream(self, object_key: str) -> AsyncIterator[bytes]:
        """Отдаёт файл чанками: в памяти живёт один чанк, а не файл."""
        path = self._resolve(object_key)
        size = self._config.launcher.copy_chunk_bytes
        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(size)
                if not chunk:
                    break
                yield chunk

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

        if isinstance(data, str):
            payload = data.encode()
        else:
            payload = data
        rc, _, err = await self._op(image, ["write", rel], source=self._once(payload))
        self._check(rc, err)
        return {
            "object_key": object_key,
            "url": self.URL_TEMPLATE,
        }

    async def upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Чанки уходят прямо в stdin лаунчера, а тот пишет их в образ."""
        image, rel = self._image_and_rel(object_key)
        rc, _, err = await self._op(image, ["write", rel], source=source)
        self._check(rc, err)
        return {"object_key": object_key, "url": self.URL_TEMPLATE}

    @staticmethod
    async def _once(payload: bytes) -> AsyncIterator[bytes]:
        """Готовые байты в виде источника: путь записи в образ один на всех."""
        yield payload

    async def read_file(self, object_key: str) -> bytes:
        image, rel = self._image_and_rel(object_key)
        rc, out, err = await self._op(image, ["read", rel])
        if rc == EXIT_NOT_FOUND:
            raise FileNotFoundError(object_key)
        self._check(rc, err)
        return out

    async def read_stream(self, object_key: str) -> AsyncIterator[bytes]:
        """Отдаёт файл чанками из stdout лаунчера, не собирая его в памяти.

        Первый чанк читается до отдачи наружу: пока он не получен, неизвестно,
        существует ли файл, а отвечать 404 после начала ответа уже поздно.
        """
        image, rel = self._image_and_rel(object_key)
        size = self._config.launcher.copy_chunk_bytes
        proc = await self._spawn(image, ["read", rel], with_stdin=False)
        if proc.stdout is None or proc.stderr is None:
            raise StorageError("storage: launcher process has no pipes")

        err_task = asyncio.create_task(proc.stderr.read())
        try:
            head = await proc.stdout.read(size)
            if not head:
                rc = await proc.wait()
                if rc == EXIT_NOT_FOUND:
                    raise FileNotFoundError(object_key)
                self._check(rc, self._drain_launcher_log(await err_task))
                return

            yield head
            while True:
                chunk = await proc.stdout.read(size)
                if not chunk:
                    break
                yield chunk

            rc = await proc.wait()
            self._check(rc, self._drain_launcher_log(await err_task))
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await asyncio.gather(err_task, return_exceptions=True)

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

    async def _spawn(
        self,
        image: str,
        op: list[str],
        with_stdin: bool,
    ) -> asyncio.subprocess.Process:
        """Запускает лаунчер на одну операцию над образом."""
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
        stdin = asyncio.subprocess.DEVNULL
        if with_stdin:
            stdin = asyncio.subprocess.PIPE
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _op(
        self,
        image: str,
        op: list[str],
        source: AsyncIterator[bytes] | None = None,
    ) -> tuple[int, bytes, bytes]:
        proc = await self._spawn(image, op, with_stdin=source is not None)
        started = time.monotonic()
        logger.info("storage: operation %s in image %s", op, image)
        try:
            out, err = await asyncio.wait_for(
                self._exchange(proc, source),
                timeout=self._config.op_timeout_sec,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            msg = (
                f"storage: image operation {op[0]!r} did not finish "
                f"within {self._config.op_timeout_sec}s"
            )
            raise StorageError(msg) from None
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

    @classmethod
    async def _exchange(
        cls,
        proc: asyncio.subprocess.Process,
        source: AsyncIterator[bytes] | None,
    ) -> tuple[bytes, bytes]:
        """communicate для источника-итератора: пайпы вычитываются, пока идёт запись."""
        if proc.stdout is None or proc.stderr is None:
            raise StorageError("storage: launcher process has no pipes")

        out_task = asyncio.create_task(proc.stdout.read())
        err_task = asyncio.create_task(proc.stderr.read())
        if source is not None:
            await cls._feed(proc.stdin, source)
        out = await out_task
        err = await err_task
        await proc.wait()
        return out, err

    @staticmethod
    async def _feed(
        stdin: asyncio.StreamWriter | None,
        source: AsyncIterator[bytes],
    ) -> None:
        """Чанк записан — забыт: в памяти живёт один чанк, а не файл."""
        if stdin is None:
            return
        try:
            async for chunk in source:
                stdin.write(chunk)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # лаунчер закрыл stdin раньше времени: причину скажет его код возврата
            logger.info("storage: launcher stopped reading input")
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                stdin.close()
                await stdin.wait_closed()

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

    @classmethod
    def _check(cls, rc: int, err: bytes) -> None:
        if rc == 0:
            return
        detail = err.decode("utf-8", errors="replace").strip()
        reason = cls._reason(detail)
        if rc == EXIT_NO_SPACE:
            msg = f"storage: {reason or 'no space left in the workspace image'}"
            raise StorageFullError(msg)
        if rc == EXIT_MOUNT_ERROR and LAUNCHER_ERROR_PREFIX in detail:
            msg = f"storage: image not mounted: {reason}"
            raise StorageError(msg)
        msg = f"storage: image operation exited with code {rc}: {detail}"
        raise StorageError(msg)

    @staticmethod
    def _reason(detail: str) -> str:
        """Своя строка лаунчера, без чужого шума в stderr соседних процессов."""
        marked: list[str] = []
        for line in detail.splitlines():
            if line.startswith(LAUNCHER_ERROR_PREFIX):
                marked.append(line[len(LAUNCHER_ERROR_PREFIX) :])
        return " ".join(marked) or detail
