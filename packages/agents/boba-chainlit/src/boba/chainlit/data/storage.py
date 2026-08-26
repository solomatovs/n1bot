"""Клиенты хранилища вложений: локальный диск и ext4-образ пользователя.

Граница ответственности слоя — дать читать потоково и не более того:
open_stream отдаёт размер объекта до первого байта и тело окном чанков.
Накапливать содержимое в памяти слой не умеет намеренно — это решает
вызывающий компонент, потому что только он знает свой предел на объём и что
делать при его превышении.

Ошибки: StorageError — операция не выполнена; StorageFullError — в образе нет
места; StorageNotFoundError — объекта нет в хранилище. Ничего сверх этого
списка наружу не выходит: системные и чужие исключения упаковывает
StorageGuard, через который проходит каждая операция StorageClient. Клиенту
достаточно ловить StorageError — подклассы разбираются, когда отказ надо
показать по-разному (404 против сбоя, нет места против прочего).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat as stat_module
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from pathlib import Path
from typing import Any, ClassVar

import aiofiles
import aiofiles.os

from boba.canvas.keys import DirKey, ObjectKey
from boba.canvas.storage import (
    FileStat,
    LauncherRead,
    OpenedStream,
    OpProgress,
    StorageError,
    StorageFullError,
    StorageGuard,
    StorageNotFoundError,
    StorageOp,
    StorageUrl,
)
from boba.chainlit.domain.config import LocalStorageConfig
from boba.sandbox import WorkspaceSpec
from boba.workspace.launcher import (
    ImageMountPoint,
    LauncherExit,
    LauncherMarker,
    LauncherMode,
    ReadHeader,
    ReadWindow,
    ResourceLimits,
    build_chain_argv,
    require_fuse,
)
from chainlit.data.storage_clients.base import BaseStorageClient

__all__ = [
    "ImageStorageClient",
    "LocalStorageClient",
    "StorageClient",
    "StorageFactory",
]

logger = logging.getLogger(__name__)


class StorageClient(BaseStorageClient, ABC):
    """Фасад хранилища: потоковые операции и их граница ошибок.

    Каждая операция идёт через StorageGuard, поэтому наружу выходят только
    StorageError и его подклассы:

    * upload_file, upload_stream — StorageFullError, когда места нет;
      StorageError на любом другом отказе записи;
    * stat, open_stream — StorageNotFoundError, когда объекта нет;
      StorageError на отказе чтения;
    * delete_file — False, когда объекта нет; StorageError на отказе;
    * list_dir — пустая последовательность, когда каталога нет;
      StorageError на отказе.

    Реализации пишут операции в защищённых методах и публичные не
    переопределяют — иначе ошибка уйдёт мимо границы.
    """

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

    @staticmethod
    async def _once(payload: bytes) -> AsyncGenerator[bytes, None]:
        """Готовые байты в виде источника: путь записи один на всех."""
        yield payload

    async def upload_file(
        self,
        object_key: str,
        data: bytes | str,
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> dict[str, Any]:
        """Байты из памяти тем же потоковым путём, что и upload_stream."""
        with StorageGuard(StorageOp.WRITE, object_key):
            keep = False
            if not overwrite:
                keep = await self._exists(object_key)

            if keep:
                return self._uploaded(object_key)

            payload = self._payload_bytes(data)
            return await self._upload_stream(object_key, self._once(payload), mime)

    async def upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        with StorageGuard(StorageOp.WRITE, object_key):
            return await self._upload_stream(object_key, source, mime)

    async def stat(self, object_key: str) -> FileStat:
        with StorageGuard(StorageOp.STAT, object_key):
            return await self._stat(object_key)

    async def open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        """Открывает объект на чтение: существование и размер — до тела."""
        with StorageGuard(StorageOp.READ, object_key):
            opened = await self._open_stream(object_key, window)

        body = self._guarded_chunks(object_key, opened.chunks)
        return OpenedStream(stat=opened.stat, chunks=body, release=opened.release)

    @staticmethod
    async def _no_release() -> None:
        """Поток без своего процесса: освобождать нечего."""
        return

    async def disk_source(self, path: str) -> AsyncGenerator[bytes, None]:
        """Файл локального диска как источник чанков для upload_stream."""
        chunk_bytes = self._config.mounting.copy_chunk_bytes

        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(chunk_bytes)
                if not chunk:
                    break

                yield chunk

    async def delete_file(self, object_key: str) -> bool:
        with StorageGuard(StorageOp.DELETE, object_key):
            return await self._delete_file(object_key)

    async def list_dir(self, prefix: str) -> Sequence[str]:
        with StorageGuard(StorageOp.LIST, prefix):
            return await self._list_dir(prefix)

    async def _exists(self, object_key: str) -> bool:
        try:
            await self._stat(object_key)
        except (StorageNotFoundError, FileNotFoundError):
            return False

        return True

    async def _guarded_chunks(
        self, object_key: str, chunks: AsyncGenerator[bytes, None]
    ) -> AsyncGenerator[bytes, None]:
        """Тело итерируется после возврата open_stream: границе нужен свой guard."""
        with StorageGuard(StorageOp.READ, object_key):
            async for chunk in chunks:
                yield chunk

    @abstractmethod
    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def _stat(self, object_key: str) -> FileStat: ...

    @abstractmethod
    async def _open_stream(
        self, object_key: str, window: ReadWindow
    ) -> OpenedStream: ...

    @abstractmethod
    async def _delete_file(self, object_key: str) -> bool: ...

    @abstractmethod
    async def _list_dir(self, prefix: str) -> Sequence[str]: ...


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
            raise StorageError(f"storage: object_key outside files_dir: {object_key!r}")

        return path

    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]:
        """Кладёт файл чанками: содержимое целиком в память не поднимается."""
        path = self._resolve(object_key)
        await aiofiles.os.makedirs(path.parent, exist_ok=True)

        async with aiofiles.open(path, "wb") as f:
            async for chunk in source:
                await f.write(chunk)

        return self._uploaded(object_key)

    async def _stat(self, object_key: str) -> FileStat:
        path = self._resolve(object_key)
        return await self._stat_path(path)

    async def _open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        path = self._resolve(object_key)

        stat = await self._stat_path(path)
        body = self._window_chunks(path, window, stat.size)
        return OpenedStream(stat=stat, chunks=body, release=self._no_release)

    @staticmethod
    async def _stat_path(path: Path) -> FileStat:
        result = await aiofiles.os.stat(path)

        if not stat_module.S_ISREG(result.st_mode):
            raise StorageError(f"storage: not a regular file: {path.name}")

        return FileStat(size=result.st_size, revision=result.st_mtime_ns)

    async def _window_chunks(
        self, path: Path, window: ReadWindow, size: int
    ) -> AsyncGenerator[bytes, None]:
        """Отдаёт окно файла чанками: в памяти живёт один чанк, а не файл."""
        chunk_bytes = self._config.mounting.copy_chunk_bytes
        remaining = window.resolve_length(size)

        async with aiofiles.open(path, "rb") as f:
            await f.seek(window.offset)

            while remaining > 0:
                chunk = await f.read(min(chunk_bytes, remaining))
                if not chunk:
                    break

                remaining -= len(chunk)
                yield chunk

    async def _delete_file(self, object_key: str) -> bool:
        path = self._resolve(object_key)
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            return False

        return True

    async def _list_dir(self, prefix: str) -> Sequence[str]:
        path = self._resolve(prefix)
        try:
            entries = await aiofiles.os.listdir(path)
        except (FileNotFoundError, NotADirectoryError):
            return ()

        names: list[str] = []
        for entry in sorted(entries):
            if not await aiofiles.os.path.isfile(path / entry):
                continue
            names.append(entry)

        return tuple(names)


class ImageStorageClient(StorageClient):
    """Хранит вложения внутри per-thread ext4-образа: fuse2fs на одну операцию."""

    WATCH_POLL_SEC: ClassVar[float] = 1.0
    """Период проверки простоя идущей операции."""

    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]:
        """Чанки уходят прямо в stdin лаунчера, а тот пишет их в образ."""
        image, rel = self._image_and_rel(object_key)
        rc, _, err = await self._op(
            image, [LauncherMode.WRITE.value, rel], source=source
        )
        self._check(rc, err)
        return self._uploaded(object_key)

    async def _stat(self, object_key: str) -> FileStat:
        image, rel = self._image_and_rel(object_key)

        rc, out, err = await self._op(image, [LauncherMode.STAT.value, rel])
        if rc == LauncherExit.NOT_FOUND:
            raise StorageNotFoundError(f"storage: no such object: {object_key}")

        self._check(rc, err)

        head = ReadHeader.parse(self._header_line(out))
        return FileStat(size=head.size, revision=head.revision)

    async def _open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        """Заголовок с размером читается до отдачи наружу: пока его нет,
        неизвестно, существует ли файл, а отвечать 404 после начала тела поздно."""
        image, rel = self._image_and_rel(object_key)
        op = [LauncherMode.READ.value, rel, *window.to_argv()]
        proc = await self._spawn(image, op, with_stdin=False)

        stdout = proc.stdout
        stderr = proc.stderr
        if stdout is None or stderr is None:
            proc.kill()
            await proc.wait()
            raise StorageError("storage: launcher process has no pipes")

        reader = LauncherRead(proc, stdout, asyncio.create_task(stderr.read()))
        try:
            header = await self._read_header(reader, object_key)
        except BaseException:
            await reader.release()
            raise

        head = ReadHeader.parse(header)
        stat = FileStat(size=head.size, revision=head.revision)
        body = self._body_chunks(reader, object_key)
        return OpenedStream(stat=stat, chunks=body, release=reader.release)

    async def _read_header(self, reader: LauncherRead, object_key: str) -> bytes:
        timeout = self._config.op_timeout_sec

        try:
            header = await asyncio.wait_for(reader.stdout.readline(), timeout)
        except TimeoutError as e:
            msg = f"storage: open of {object_key} stalled for {timeout}s"
            raise StorageError(msg) from e

        if header:
            return header

        # пустой stdout: лаунчер завершился, не дойдя до файла
        rc = await reader.proc.wait()
        if rc == LauncherExit.NOT_FOUND:
            raise StorageNotFoundError(f"storage: no such object: {object_key}")

        self._check(rc, self._drain_launcher_log(await reader.stderr))
        raise StorageError(f"storage: launcher sent no read header: {object_key}")

    async def _body_chunks(
        self, reader: LauncherRead, object_key: str
    ) -> AsyncGenerator[bytes, None]:
        """Тело окна из stdout лаунчера; простой дольше таймаута — зависание."""
        chunk_bytes = self._config.mounting.copy_chunk_bytes
        timeout = self._config.op_timeout_sec

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.stdout.read(chunk_bytes), timeout
                    )
                except TimeoutError as e:
                    msg = f"storage: read of {object_key} stalled for {timeout}s"
                    raise StorageError(msg) from e

                if not chunk:
                    break

                yield chunk

            rc = await reader.proc.wait()
            self._check(rc, self._drain_launcher_log(await reader.stderr))
        finally:
            await reader.release()

    @staticmethod
    def _header_line(out: bytes) -> bytes:
        line, _, _ = out.partition(b"\n")
        return line

    async def _delete_file(self, object_key: str) -> bool:
        image, rel = self._image_and_rel(object_key)
        rc, _, err = await self._op(image, [LauncherMode.DELETE.value, rel])
        if rc == LauncherExit.NOT_FOUND:
            return False

        self._check(rc, err)

        return True

    async def _list_dir(self, prefix: str) -> Sequence[str]:
        key = DirKey.parse(prefix)
        image = self._workspace().image_of(key.user_id)

        op = [LauncherMode.LIST.value, key.in_thread()]
        rc, out, err = await self._op(image, op)
        if rc == LauncherExit.NOT_FOUND:
            return ()

        self._check(rc, err)

        names: list[str] = []
        for line in out.decode("utf-8").splitlines():
            name = line.strip()
            if not name:
                continue
            names.append(name)

        return tuple(names)

    def _workspace(self) -> WorkspaceSpec:
        """Запись workspace конфига; её отсутствие отсекает валидация секции."""
        workspace = self._config.workspace
        if workspace is None:
            msg = "storage: kind=image without the workspace record"
            raise StorageError(msg)

        return workspace

    def _image_and_rel(self, object_key: str) -> tuple[str, str]:
        key = ObjectKey.parse(object_key)
        # образ общий на пользователя: thread_id остаётся частью пути внутри
        return self._workspace().image_of(key.user_id), key.in_thread()

    async def _spawn(
        self,
        image: str,
        op: list[str],
        with_stdin: bool,
    ) -> asyncio.subprocess.Process:
        """Запускает лаунчер на одну операцию над образом."""
        require_fuse(self._config.binaries)
        await aiofiles.os.makedirs(os.path.dirname(image), exist_ok=True)
        argv = build_chain_argv(
            images=[(image, ImageMountPoint.under(self._config.mount_dir, image))],
            template=self._workspace().template,
            op=op,
            python_bin=sys.executable,
            options=self._config.mounting.to_options(),
            limits=ResourceLimits(),
            binaries=self._config.binaries,
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
        # reason вместо detail: в stderr попадает и болтовня fuse2fs
        msg = f"storage: image operation exited with code {rc}: {reason}"
        raise StorageError(msg)

    @staticmethod
    def _reason(detail: str) -> str:
        """Своя строка лаунчера, без чужого шума в stderr соседних процессов."""
        marked: list[str] = []
        for line in detail.splitlines():
            if line.startswith(LauncherMarker.ERROR.value):
                marked.append(line[len(LauncherMarker.ERROR) :])
        return " ".join(marked) or detail
