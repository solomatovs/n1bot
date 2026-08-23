"""Потоковость хранилища на реальных данных: окна, объём и расход памяти.

Проверяется главное свойство слоя: память процесса не зависит от размера
файла. Данные — детерминированный паттерн, байт по абсолютной позиции i
равен i % 256, поэтому содержимое любого окна вычисляется без хранения
файла в памяти.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import resource
import shutil
import subprocess
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Self

import pytest

from boba.chainlit.data.storage import (
    ImageStorageClient,
    OpenedStream,
    StorageFactory,
)
from boba.chainlit.infra.config import LocalStorageConfig
from boba.workspace.launcher import FUSE_DEVICE, ReadWindow

needs_fuse = pytest.mark.skipif(
    shutil.which("bwrap") is None
    or shutil.which("fuse2fs") is None
    or shutil.which("mkfs.ext4") is None
    or not os.path.exists(FUSE_DEVICE),
    reason="нужны bwrap, fuse2fs, mkfs.ext4 и /dev/fuse",
)

KEY = "7/t1/upload/big.bin"


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


class PatternPayload:
    """Детерминированные данные: байт по позиции i равен i % 256.

    Период совпадает с размером блока, поэтому содержимое окна считается
    формулой, а сам файл в памяти теста не живёт.
    """

    PERIOD: ClassVar[int] = 256
    BLOCK_BYTES: ClassVar[int] = 1 << 20

    def __init__(self, blocks: int) -> None:
        self._blocks = blocks
        self._block = bytes(range(self.PERIOD)) * (self.BLOCK_BYTES // self.PERIOD)

    @property
    def size(self) -> int:
        return self._blocks * self.BLOCK_BYTES

    async def source(self) -> AsyncIterator[bytes]:
        """Источник для upload_stream: наружу уходит один и тот же блок."""
        for _ in range(self._blocks):
            yield self._block

    def digest(self) -> str:
        """Хеш всего содержимого, посчитанный поблочно."""
        state = hashlib.sha256()
        for _ in range(self._blocks):
            state.update(self._block)

        return state.hexdigest()

    @classmethod
    def slice_at(cls, offset: int, length: int) -> bytes:
        stop = offset + length
        return bytes(i % cls.PERIOD for i in range(offset, stop))


class MemoryProbe:
    """Пиковая память python-процесса за время блока."""

    def __init__(self) -> None:
        self.peak_bytes = 0

    def __enter__(self) -> Self:
        tracemalloc.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.peak_bytes = peak


class ChildMemory:
    """Пиковый RSS дочерних процессов: у лаунчера он не зависит от объёма."""

    @staticmethod
    def peak_bytes() -> int:
        return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(scope="module")
def big_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Образ на 512 МиБ: разрежённый, поэтому создаётся мгновенно."""
    path = tmp_path_factory.mktemp("streaming") / "template.ext4"
    with path.open("wb") as f:
        f.truncate(512 * 1024 * 1024)

    mkfs = shutil.which("mkfs.ext4")
    if mkfs is None:
        raise AssertionError("mkfs is not None")
    subprocess.run(  # noqa: S603
        [mkfs, "-F", "-q", "-O", "^has_journal", "-m", "0", str(path)],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def payload() -> PatternPayload:
    return PatternPayload(blocks=128)


@pytest.fixture(scope="module")
def storage(
    tmp_path_factory: pytest.TempPathFactory,
    big_template: Path,
    payload: PatternPayload,
) -> ImageStorageClient:
    """Образ с уже залитым файлом: он один на все проверки чтения."""
    root = tmp_path_factory.mktemp("image")
    fields: dict[str, Any] = {
        "kind": "image",
        "mount_dir": "/tmp",  # noqa: S108
        "workspace": {
            "template": str(big_template),
            "mount": f"{root}/ws/{{user_id}}.ext4:/workspace",
        },
        "op_timeout_sec": 120,
        "mounting": {
            "mount_wait_sec": 10.0,
            "mount_poll_sec": 0.05,
            "shutdown_wait_sec": 5.0,
            "lock_wait_sec": 10.0,
            "copy_chunk_bytes": 1 << 20,
        },
        "binaries": {"dirs": _bin_dirs()},
    }
    client = StorageFactory.create(LocalStorageConfig.model_validate(fields))
    if not (isinstance(client, ImageStorageClient)):
        raise AssertionError("isinstance(client, ImageStorageClient)")

    asyncio.run(client.upload_stream(KEY, payload.source()))

    return client


async def drain(storage: ImageStorageClient, window: ReadWindow) -> tuple[int, str]:
    """Считывает окно, не накапливая его: наружу идут объём и хеш."""
    opened = await storage.open_stream(KEY, window)

    state = hashlib.sha256()
    read = 0
    async for chunk in opened.chunks:
        read += len(chunk)
        state.update(chunk)

    return read, state.hexdigest()


async def body_of(opened: OpenedStream) -> bytes:
    collected = bytearray()
    async for chunk in opened.chunks:
        collected.extend(chunk)

    return bytes(collected)


async def collect(storage: ImageStorageClient, window: ReadWindow) -> bytes:
    opened = await storage.open_stream(KEY, window)
    return await body_of(opened)


@needs_fuse
class TestStreamingReads:
    """Чтение из образа: содержимое верное, память постоянная."""

    HOST_PEAK_LIMIT: ClassVar[int] = 8 << 20
    """Потолок пика хоста: чанк 1 МиБ, запас на восемь чанков."""

    def test_upload_does_not_buffer_the_file(
        self,
        tmp_path: Path,
        big_template: Path,
        payload: PatternPayload,
    ) -> None:
        """Заливка 128 МиБ: в памяти живёт блок, а не файл."""
        fields: dict[str, Any] = {
            "kind": "image",
            "mount_dir": "/tmp",  # noqa: S108
            "workspace": {
                "template": str(big_template),
                "mount": f"{tmp_path}/ws/{{user_id}}.ext4:/workspace",
            },
            "op_timeout_sec": 120,
            "mounting": {
                "mount_wait_sec": 10.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 5.0,
                "lock_wait_sec": 10.0,
                "copy_chunk_bytes": 1 << 20,
            },
            "binaries": {"dirs": _bin_dirs()},
        }
        client = StorageFactory.create(LocalStorageConfig.model_validate(fields))

        with MemoryProbe() as probe:
            asyncio.run(client.upload_stream(KEY, payload.source()))

        stat = asyncio.run(client.stat(KEY))
        if stat.size != payload.size:
            raise AssertionError("stat.size == payload.size")
        if probe.peak_bytes >= self.HOST_PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.HOST_PEAK_LIMIT")

    def test_whole_file_streams_without_buffering(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        """Чтение 128 МиБ целиком: содержимое сходится, память не растёт."""
        with MemoryProbe() as probe:
            read, digest = asyncio.run(drain(storage, ReadWindow.entire()))

        if read != payload.size:
            raise AssertionError("read == payload.size")
        if digest != payload.digest():
            raise AssertionError("digest == payload.digest()")
        if probe.peak_bytes >= self.HOST_PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.HOST_PEAK_LIMIT")

    def test_stat_reports_size_without_reading_body(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        with MemoryProbe() as probe:
            stat = asyncio.run(storage.stat(KEY))

        if stat.size != payload.size:
            raise AssertionError("stat.size == payload.size")
        if probe.peak_bytes >= self.HOST_PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.HOST_PEAK_LIMIT")

    def test_window_reads_only_its_own_bytes(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        """Окно в конце файла: отдаётся ровно оно, а не хвост с начала."""
        offset = 100 * PatternPayload.BLOCK_BYTES + 7
        length = 4096

        with MemoryProbe() as probe:
            got = asyncio.run(
                collect(storage, ReadWindow(offset=offset, length=length))
            )

        if got != PatternPayload.slice_at(offset, length):
            raise AssertionError("got == PatternPayload.slice_at(offset, length)")
        if probe.peak_bytes >= self.HOST_PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.HOST_PEAK_LIMIT")

    def test_windows_walk_the_file_forward_and_back(
        self, storage: ImageStorageClient
    ) -> None:
        """Прокрутка бегунка: окна идут вразнобой, каждое читается само по себе."""
        length = 8192
        offsets = [
            0,
            64 * PatternPayload.BLOCK_BYTES + 13,
            127 * PatternPayload.BLOCK_BYTES,
            3 * PatternPayload.BLOCK_BYTES + 1,
        ]

        for offset in offsets:
            window = ReadWindow(offset=offset, length=length)
            got = asyncio.run(collect(storage, window))
            if got != PatternPayload.slice_at(offset, length):
                raise AssertionError(f"offset {offset}")

    def test_window_past_the_end_is_empty(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        window = ReadWindow(offset=payload.size + 1024, length=4096)
        read, _ = asyncio.run(drain(storage, window))

        if read != 0:
            raise AssertionError("read == 0")

    def test_tail_window_runs_to_the_end(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        offset = payload.size - 5000
        window = ReadWindow(offset=offset, length=None)

        got = asyncio.run(collect(storage, window))

        if got != PatternPayload.slice_at(offset, 5000):
            raise AssertionError("got == PatternPayload.slice_at(offset, 5000)")

    def test_size_lets_the_caller_refuse_before_the_body(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        """Потолок ставит вызывающий: размер известен, а тело так и не читается."""

        async def peek_and_refuse() -> int:
            async with await storage.open_stream(KEY, ReadWindow.entire()) as body:
                return body.stat.size

        with MemoryProbe() as probe:
            size = asyncio.run(peek_and_refuse())

        if size != payload.size:
            raise AssertionError("size == payload.size")
        if probe.peak_bytes >= self.HOST_PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.HOST_PEAK_LIMIT")

    def test_launcher_memory_does_not_scale_with_the_file(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        """RSS лаунчера после мелкого и после полного чтения — один и тот же."""
        asyncio.run(collect(storage, ReadWindow(offset=0, length=4096)))
        after_small = ChildMemory.peak_bytes()

        read, _ = asyncio.run(drain(storage, ReadWindow.entire()))
        after_whole = ChildMemory.peak_bytes()

        if read != payload.size:
            raise AssertionError("read == payload.size")
        if after_whole != after_small:
            raise AssertionError("after_whole == after_small")

    def test_abandoned_stream_releases_the_image(
        self, storage: ImageStorageClient, payload: PatternPayload
    ) -> None:
        """Клиент ушёл на первом чанке: лаунчер снят, лок образа отдан.

        Если лок утечёт, следующая запись будет ждать его вечно и тест повиснет.
        """

        async def abandon_then_write() -> int:
            opened = await storage.open_stream(KEY, ReadWindow.entire())

            async for _ in opened.chunks:
                break

            await opened.close()

            await storage.upload_file("7/t1/upload/after.txt", b"lock released")
            return (await storage.stat("7/t1/upload/after.txt")).size

        if asyncio.run(abandon_then_write()) != len(b"lock released"):
            raise AssertionError('asyncio.run(abandon_then_write()) == len(b"lock rel…')

    def test_reader_does_not_block_another_reader(
        self, storage: ImageStorageClient
    ) -> None:
        """Разделяемый лок: пока одно окно открыто, второе читается без ожидания."""

        async def overlap() -> bytes:
            held = await storage.open_stream(KEY, ReadWindow.entire())
            async for _ in held.chunks:
                break

            try:
                window = ReadWindow(offset=0, length=4096)
                return await asyncio.wait_for(collect(storage, window), timeout=60)
            finally:
                await held.close()

        if asyncio.run(overlap()) != PatternPayload.slice_at(0, 4096):
            raise AssertionError("asyncio.run(overlap()) == PatternPayload.slice_at(0…")

    OVERLAP_TIMEOUT_SEC: ClassVar[float] = 30.0
    """Второе открытие под эксклюзивным локом ждало бы вечно: лучше упасть."""

    def test_unread_stream_releases_the_image(
        self, tmp_path: Path, big_template: Path, payload: PatternPayload
    ) -> None:
        """Поток закрыт, не прочитав ни чанка: лок на образе не остался висеть.

        Отдельный случай от брошенного на середине: тело ни разу не двигали,
        поэтому его finally не исполнится и снимать лаунчер должен close.
        """
        fields: dict[str, Any] = {
            "kind": "image",
            "mount_dir": "/tmp",  # noqa: S108
            "workspace": {
                "template": str(big_template),
                "mount": f"{tmp_path}/ws/{{user_id}}.ext4:/workspace",
            },
            "op_timeout_sec": 30,
            "mounting": {
                "mount_wait_sec": 10.0,
                "mount_poll_sec": 0.05,
                "shutdown_wait_sec": 5.0,
                "lock_wait_sec": 10.0,
                "copy_chunk_bytes": 1 << 20,
            },
            "binaries": {"dirs": _bin_dirs()},
        }
        client = StorageFactory.create(LocalStorageConfig.model_validate(fields))

        async def abandon_then_write() -> int:
            await client.upload_stream(KEY, payload.source())

            opened = await client.open_stream(KEY, ReadWindow.entire())
            await opened.close()

            writing = client.upload_file("7/t1/upload/after.txt", b"x")
            await asyncio.wait_for(writing, self.OVERLAP_TIMEOUT_SEC)

            stat = await client.stat("7/t1/upload/after.txt")
            return stat.size

        if asyncio.run(abandon_then_write()) != 1:
            raise AssertionError("asyncio.run(abandon_then_write()) == 1")

    def test_concurrent_window_reads_share_the_image(
        self, storage: ImageStorageClient
    ) -> None:
        """Разделяемый лок: окна из одного образа читаются одновременно."""
        length = 4096
        offsets = [0, 50 * PatternPayload.BLOCK_BYTES, 120 * PatternPayload.BLOCK_BYTES]

        async def race() -> list[bytes]:
            reads = []
            for offset in offsets:
                window = ReadWindow(offset=offset, length=length)
                reads.append(collect(storage, window))

            return list(await asyncio.gather(*reads))

        results = asyncio.run(race())

        for offset, got in zip(offsets, results, strict=True):
            if got != PatternPayload.slice_at(offset, length):
                raise AssertionError(f"offset {offset}")
