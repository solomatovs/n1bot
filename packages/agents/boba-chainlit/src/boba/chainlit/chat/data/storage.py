"""Клиенты хранилища вложений: локальный диск и ext4-образ пользователя.

Ошибки: StorageError — операция не выполнена; StorageFullError — в образе нет
места; FileNotFoundError — файла нет в хранилище.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import aiofiles
import aiofiles.os

from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.infra.config import LocalStorageConfig
from boba.workspace.launcher import (
    LauncherExit,
    LauncherMarker,
    LauncherMode,
    ResourceLimits,
    build_chain_argv,
    render_image_path,
    require_fuse,
)
from chainlit.data.storage_clients.base import BaseStorageClient

__all__ = [
    "ImageStorageClient",
    "LocalStorageClient",
    "StorageClient",
    "StorageError",
    "StorageFactory",
    "StorageFullError",
    "StorageUrl",
]

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Хранилище не выполнило операцию."""


class StorageFullError(StorageError):
    """В образе пользователя не осталось места под файл."""


class StorageUrl(StrEnum):
    """Шаблон хранимого url вложения: сам ключ живёт в object_key."""

    PREFIX = "{public_prefix}"
    KEY = "{object_key}"
    TEMPLATE = "{public_prefix}/{object_key}"

    @classmethod
    def render(cls, url: str, public_prefix: str, object_key: str) -> str:
        rendered = url.replace(cls.PREFIX, public_prefix.rstrip("/"))
        return rendered.replace(cls.KEY, object_key)


class StorageClient(BaseStorageClient, ABC):
    """База клиентов хранилища вложений."""

    def __init__(self, config: LocalStorageConfig) -> None:
        self._config = config

    def render_url(self, url: str, object_key: str) -> str:
        return StorageUrl.render(url, self._config.public_prefix, object_key)

    async def get_read_url(self, object_key: str) -> str:
        return self.render_url(StorageUrl.TEMPLATE, object_key)

    async def close(self) -> None:
        pass

    @staticmethod
    def _payload_bytes(data: bytes | str) -> bytes:
        if isinstance(data, str):
            return data.encode()
        return data

    @staticmethod
    def _uploaded(object_key: str) -> dict[str, Any]:
        return {"object_key": object_key, "url": StorageUrl.TEMPLATE.value}

    @abstractmethod
    async def upload_file(
        self,
        object_key: str,
        data: bytes | str,
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def read_file(self, object_key: str) -> bytes: ...

    @abstractmethod
    def read_stream(self, object_key: str) -> AsyncIterator[bytes]: ...

    @abstractmethod
    async def delete_file(self, object_key: str) -> bool: ...


class StorageFactory:
    """Выбирает реализацию хранилища по конфигу."""

    @staticmethod
    def create(config: LocalStorageConfig) -> StorageClient:
        if config.kind == "image":
            return ImageStorageClient(config)

        return LocalStorageClient(config)


class LocalStorageClient(StorageClient):
    """Хранит файлы вложений на локальном диске под files_dir."""

    def _resolve(self, object_key: str) -> Path:
        base_dir = Path(self._config.files_dir).resolve()
        path = (base_dir / object_key).resolve()

        if not path.is_relative_to(base_dir):
            raise ValueError(f"object_key outside files_dir: {object_key!r}")

        return path

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
            return self._uploaded(object_key)

        await aiofiles.os.makedirs(path.parent, exist_ok=True)

        payload = self._payload_bytes(data)

        async with aiofiles.open(path, "wb") as f:
            await f.write(payload)

        return self._uploaded(object_key)

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

        return self._uploaded(object_key)

    async def read_file(self, object_key: str) -> bytes:
        path = self._resolve(object_key)
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def read_stream(self, object_key: str) -> AsyncGenerator[bytes, None]:
        """Отдаёт файл чанками: в памяти живёт один чанк, а не файл."""
        path = self._resolve(object_key)
        size = self._config.launcher.copy_chunk_bytes
        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(size)
                if not chunk:
                    break
                yield chunk

    async def delete_file(self, object_key: str) -> bool:
        path = self._resolve(object_key)
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            return False
        return True


class OpProgress:
    """Отметки прогресса операции: по ним таймаут отличает зависание от работы."""

    def __init__(self) -> None:
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    def idle_sec(self) -> float:
        return time.monotonic() - self._last


class ImageStorageClient(StorageClient):
    """Хранит вложения внутри per-thread ext4-образа: fuse2fs на одну операцию."""

    WATCH_POLL_SEC: ClassVar[float] = 1.0
    """Период проверки простоя идущей операции."""

    async def upload_file(
        self,
        object_key: str,
        data: bytes | str,
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> dict[str, Any]:
        image, rel = self._image_and_rel(object_key)

        keep = False
        if not overwrite:
            keep = await self._exists(image, rel)

        if keep:
            return self._uploaded(object_key)

        payload = self._payload_bytes(data)

        rc, _, err = await self._op(
            image, [LauncherMode.WRITE.value, rel], source=self._once(payload)
        )
        self._check(rc, err)
        return self._uploaded(object_key)

    async def upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Чанки уходят прямо в stdin лаунчера, а тот пишет их в образ."""
        image, rel = self._image_and_rel(object_key)
        rc, _, err = await self._op(
            image, [LauncherMode.WRITE.value, rel], source=source
        )
        self._check(rc, err)
        return self._uploaded(object_key)

    @staticmethod
    async def _once(payload: bytes) -> AsyncIterator[bytes]:
        """Готовые байты в виде источника: путь записи в образ один на всех."""
        yield payload

    async def read_file(self, object_key: str) -> bytes:
        image, rel = self._image_and_rel(object_key)
        rc, out, err = await self._op(image, [LauncherMode.READ.value, rel])
        if rc == LauncherExit.NOT_FOUND:
            raise FileNotFoundError(object_key)
        self._check(rc, err)
        return out

    async def read_stream(self, object_key: str) -> AsyncGenerator[bytes, None]:
        """Отдаёт файл чанками из stdout лаунчера, не собирая его в памяти.

        Первый чанк читается до отдачи наружу: пока он не получен, неизвестно,
        существует ли файл, а отвечать 404 после начала ответа уже поздно.
        """
        image, rel = self._image_and_rel(object_key)
        size = self._config.launcher.copy_chunk_bytes
        proc = await self._spawn(
            image, [LauncherMode.READ.value, rel], with_stdin=False
        )
        if proc.stdout is None or proc.stderr is None:
            raise StorageError("storage: launcher process has no pipes")

        err_task = asyncio.create_task(proc.stderr.read())
        try:
            head = await proc.stdout.read(size)
            if not head:
                rc = await proc.wait()
                if rc == LauncherExit.NOT_FOUND:
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
        rc, _, err = await self._op(image, [LauncherMode.DELETE.value, rel])
        if rc == LauncherExit.NOT_FOUND:
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
        rc, _, err = await self._op(image, [LauncherMode.READ.value, rel])
        if rc == LauncherExit.NOT_FOUND:
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
        """op_timeout_sec ловит зависание, а не медленный источник: таймер
        отсчитывается от последнего принятого чанка, не от старта операции."""
        proc = await self._spawn(image, op, with_stdin=source is not None)
        started = time.monotonic()
        logger.info("storage: operation %s in image %s", op, image)
        progress = OpProgress()
        exchange = asyncio.create_task(self._exchange(proc, source, progress))
        try:
            out, err = await self._watch(exchange, progress)
        except TimeoutError:
            exchange.cancel()
            await asyncio.gather(exchange, return_exceptions=True)
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            msg = (
                f"storage: image operation {op[0]!r} stalled "
                f"for {self._config.op_timeout_sec}s"
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

    async def _watch(
        self,
        exchange: asyncio.Task[tuple[bytes, bytes]],
        progress: OpProgress,
    ) -> tuple[bytes, bytes]:
        """Ждёт обмен, пока тот подаёт признаки жизни; простой — TimeoutError."""
        pending = {exchange}
        while True:
            done, _ = await asyncio.wait(pending, timeout=self.WATCH_POLL_SEC)
            if done:
                return exchange.result()

            if progress.idle_sec() > self._config.op_timeout_sec:
                raise TimeoutError

    @classmethod
    async def _exchange(
        cls,
        proc: asyncio.subprocess.Process,
        source: AsyncIterator[bytes] | None,
        progress: OpProgress,
    ) -> tuple[bytes, bytes]:
        """communicate для источника-итератора: пайпы вычитываются, пока идёт запись."""
        if proc.stdout is None or proc.stderr is None:
            raise StorageError("storage: launcher process has no pipes")

        out_task = asyncio.create_task(proc.stdout.read())
        err_task = asyncio.create_task(proc.stderr.read())
        if source is not None:
            await cls._feed(proc.stdin, source, progress)
        out = await out_task
        err = await err_task
        await proc.wait()
        return out, err

    @staticmethod
    async def _feed(
        stdin: asyncio.StreamWriter | None,
        source: AsyncIterator[bytes],
        progress: OpProgress,
    ) -> None:
        """Чанк записан — забыт: в памяти живёт один чанк, а не файл."""
        if stdin is None:
            return
        try:
            async for chunk in source:
                stdin.write(chunk)
                await stdin.drain()
                progress.beat()
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
        if LauncherMarker.LOG.value not in text:
            return err
        kept: list[str] = []
        for line in text.splitlines():
            if line.startswith(LauncherMarker.LOG.value):
                logger.info("storage: %s", line[len(LauncherMarker.LOG) :])
            else:
                kept.append(line)
        return "\n".join(kept).encode("utf-8")

    @classmethod
    def _check(cls, rc: int, err: bytes) -> None:
        if rc == 0:
            return
        detail = err.decode("utf-8", errors="replace").strip()
        reason = cls._reason(detail)
        if rc == LauncherExit.NO_SPACE:
            msg = f"storage: {reason or 'no space left in the workspace image'}"
            raise StorageFullError(msg)
        if rc == LauncherExit.MOUNT_ERROR and LauncherMarker.ERROR.value in detail:
            msg = f"storage: image not mounted: {reason}"
            raise StorageError(msg)
        msg = f"storage: image operation exited with code {rc}: {detail}"
        raise StorageError(msg)

    @staticmethod
    def _reason(detail: str) -> str:
        """Своя строка лаунчера, без чужого шума в stderr соседних процессов."""
        marked: list[str] = []
        for line in detail.splitlines():
            if line.startswith(LauncherMarker.ERROR.value):
                marked.append(line[len(LauncherMarker.ERROR) :])
        return " ".join(marked) or detail
