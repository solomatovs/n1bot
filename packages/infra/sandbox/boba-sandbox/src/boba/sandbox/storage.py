"""Потоковое хранилище файлов в ext4-образах через внешний слой песочницы.

Ошибки:
StorageError — операция хранилища не выполнена.
StorageNotFoundError — объект отсутствует.
StorageFullError — в образе нет места."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any, ClassVar, Literal

from anyio import Path
from pydantic import ConfigDict, Field

from boba.canvas.keys import DirKey, ObjectKey
from boba.canvas.storage import (
    FileStat,
    LauncherRead,
    OpenedStream,
    OpProgress,
    ReadWindow,
    StorageError,
    StorageFullError,
    StorageNotFoundError,
)
from boba.sandbox import WorkspaceSpec
from boba.sandbox.paths import ProgramRoot
from boba.storage.client import StorageClient, StorageConfig
from boba.workspace.launcher import (
    ImageMountPoint,
    LauncherChain,
    LauncherExit,
    LauncherMarker,
    LauncherMode,
    LayerSources,
    MountingConfig,
    ReadHeader,
    ResourceLimits,
)

logger = logging.getLogger(__name__)


class ImageStorageConfig(StorageConfig):
    """Настройки ext4-хранилища для ImageStorageClient: монтирование и сроки
    операций. StorageFactory выбирает его по kind=image."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["image"] = "image"
    public_prefix: str = Field(default="/upload")
    op_timeout_sec: int = Field(
        default=60,
        ge=1,
        description="kind=image: таймаут одной операции с образом, сек.",
    )
    mounting: MountingConfig = Field(
        description=(
            "kind=image: тайминги и размеры операций монтирования — та же "
            "запись, что у песочницы (${sandbox.mounting})."
        ),
    )
    mount_dir: str = Field(
        min_length=1,
        description=(
            "kind=image: каталог, куда хранилище монтирует образ пользователя "
            "на время операции. Поверх него кладётся tmpfs, поэтому на хосте "
            "не остаётся ни точки монтирования, ни пустых каталогов."
        ),
    )

    @property
    def chunk_bytes(self) -> int:
        return self.mounting.copy_chunk_bytes

    def read_chunk_bytes(self) -> int:
        return self.chunk_bytes


class ImageStorageClient(StorageClient):
    """Хранит вложения внутри ext4-образа пользователя: fuse2fs на одну операцию.

    Каждая операция — цепочка лаунчера (LauncherChain) над образом
    пользователя: внешний слой видит интерпретатор, бинари, шаблон и каталог
    образов, а не корень хоста.
    """

    WATCH_POLL_SEC: ClassVar[float] = 1.0
    """Период проверки простоя идущей операции."""

    def __init__(
        self, config: ImageStorageConfig, workspace: WorkspaceSpec, program: ProgramRoot
    ) -> None:
        super().__init__(config.public_prefix, config.chunk_bytes)
        self._config = config
        self._workspace_spec = workspace
        self._chain = LauncherChain(
            program.interpreter,
            program.binaries(),
            config.mounting.to_options(),
        )

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
        self._check(rc, err, f"write of {object_key}")
        return self._uploaded(object_key)

    async def _stat(self, object_key: str) -> FileStat:
        image, rel = self._image_and_rel(object_key)

        rc, out, err = await self._op(image, [LauncherMode.STAT.value, rel])
        if rc == LauncherExit.NOT_FOUND:
            raise StorageNotFoundError(f"storage: no such object: {object_key}")

        self._check(rc, err, f"stat of {object_key}")

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
            msg = f"storage: launcher process for read of {object_key} has no pipes"
            raise StorageError(msg)

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

        self._check(
            rc, self._drain_launcher_log(await reader.stderr), f"read of {object_key}"
        )
        msg = (
            f"storage: launcher exited with code {rc} without a read header "
            f"for {object_key}"
        )
        raise StorageError(msg)

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
            self._check(
                rc,
                self._drain_launcher_log(await reader.stderr),
                f"read of {object_key}",
            )
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

        self._check(rc, err, f"delete of {object_key}")

        return True

    async def _list_dir(self, prefix: str) -> Sequence[str]:
        key = DirKey.parse(prefix)
        image = self._workspace().image_of(key.user_id)

        op = [LauncherMode.LIST.value, key.in_scope()]
        rc, out, err = await self._op(image, op)
        if rc == LauncherExit.NOT_FOUND:
            return ()

        self._check(rc, err, f"list of {prefix}")

        names: list[str] = []
        for line in out.decode("utf-8").splitlines():
            name = line.strip()
            if not name:
                continue
            names.append(name)

        return tuple(names)

    def _workspace(self) -> WorkspaceSpec:
        return self._workspace_spec

    def _image_and_rel(self, object_key: str) -> tuple[str, str]:
        key = ObjectKey.parse(object_key)
        # образ общий на пользователя: scope_id остаётся частью пути внутри
        return self._workspace().image_of(key.user_id), key.in_scope()

    async def _spawn(
        self,
        image: str,
        op: list[str],
        with_stdin: bool,
    ) -> asyncio.subprocess.Process:
        """Запускает лаунчер на одну операцию над образом."""
        self._chain.check()
        await Path(image).parent.mkdir(parents=True, exist_ok=True)
        argv = self._chain.argv(
            images=[(image, ImageMountPoint.under(self._config.mount_dir, image))],
            ro_images=(),
            template=self._workspace().template,
            op=op,
            limits=ResourceLimits(),
            pass_fds=(),
            nested=LayerSources(ro=(), rw=()),
            network=False,
            replace=False,
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
                f"storage: image operation {op[0]!r} in {image} stalled "
                f"for {self._config.op_timeout_sec}s without progress"
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
    def _check(cls, rc: int, err: bytes, action: str) -> None:
        """action — что делалось и над чем (например "write of <key>")."""
        if rc == 0:
            return

        detail = err.decode("utf-8", errors="replace").strip()
        reason = cls._reason(detail)

        if rc == LauncherExit.NO_SPACE:
            if not reason:
                reason = "no space left in the workspace image"
            msg = f"storage: {action} failed: {reason}"
            raise StorageFullError(msg)

        if rc == LauncherExit.MOUNT_ERROR and LauncherMarker.ERROR.value in detail:
            msg = f"storage: {action} failed, image not mounted: {reason}"
            raise StorageError(msg)

        # reason вместо detail: в stderr попадает и болтовня fuse2fs
        msg = f"storage: {action} failed, launcher exited with code {rc}: {reason}"
        raise StorageError(msg)

    @staticmethod
    def _reason(detail: str) -> str:
        """Своя строка лаунчера, без чужого шума в stderr соседних процессов."""
        marked: list[str] = []
        for line in detail.splitlines():
            if line.startswith(LauncherMarker.ERROR.value):
                marked.append(line[len(LauncherMarker.ERROR) :])
        return " ".join(marked) or detail
