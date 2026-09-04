"""Хранилище файлов пользователя: описание файла, охрана операций, окна чтения,
ошибки; клиенты хранилища — в приложении.

Ошибки:
StorageError — операция хранилища не выполнена.
StorageFullError — превышена квота.
StorageNotFoundError — объекта нет.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FileStat",
    "LauncherRead",
    "OpProgress",
    "OpenedStream",
    "StorageError",
    "StorageFullError",
    "StorageGuard",
    "StorageNotFoundError",
    "StorageOp",
    "StorageUrl",
]

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Хранилище не выполнило операцию: корень всех ошибок слоя."""


class StorageFullError(StorageError):
    """В образе пользователя не осталось места под файл."""


class StorageNotFoundError(StorageError):
    """Объекта с таким ключом в хранилище нет."""


class FileStat(BaseModel):
    """Свойства объекта хранилища без чтения тела.

    revision — версия содержимого (момент последней записи): по ней слежение
    канваса видит правку, не изменившую размер файла.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int = Field(ge=0)
    revision: int = Field(default=0, ge=0)


@dataclass(frozen=True, slots=True)
class OpenedStream:
    """Открытый на чтение объект: размер известен до первого байта тела.

    Держит процесс чтения и его лок на образе, поэтому закрывать обязательно;
    для этого поток сам является асинхронным контекстом — `async with`.
    release освобождает ресурсы явно, а не через финализацию генератора:
    aclose у ни разу не запущенного генератора не исполняет его тело, и
    процесс чтения остался бы жив вместе со своим локом.
    """

    stat: FileStat
    chunks: AsyncGenerator[bytes, None]
    release: Callable[[], Awaitable[None]]

    async def close(self) -> None:
        """Бросить поток, не дочитывая: и генератор, и процесс освобождаются."""
        await self.chunks.aclose()
        await self.release()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


class StorageOp(StrEnum):
    """Операции хранилища: имя уходит в текст ошибки."""

    READ = "read"
    STAT = "stat"
    WRITE = "write"
    DELETE = "delete"
    LIST = "list"


class StorageGuard:
    """Граница слоя: наружу выпускает только ошибки хранилища.

    FileNotFoundError — StorageNotFoundError, прочий OSError и любое
    неожиданное исключение — StorageError; готовая ошибка слоя проходит как
    есть. Отмена задачи (BaseException) не трогается.
    """

    def __init__(self, op: StorageOp, object_key: str) -> None:
        self._op = op
        self._key = object_key

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is None:
            return

        if isinstance(exc, StorageError):
            return

        if isinstance(exc, FileNotFoundError):
            msg = f"storage: {self._op} target not found: {self._key}: {exc}"
            raise StorageNotFoundError(msg) from exc

        if isinstance(exc, OSError):
            msg = f"storage: {self._op} failed: {self._key}: {exc}"
            raise StorageError(msg) from exc

        if isinstance(exc, Exception):
            msg = f"storage: unexpected failure on {self._op}: {self._key}: {exc}"
            raise StorageError(msg) from exc


class StorageUrl(StrEnum):
    """Шаблон хранимого url вложения: сам ключ живёт в object_key."""

    PREFIX = "{public_prefix}"
    KEY = "{object_key}"
    TEMPLATE = "{public_prefix}/{object_key}"

    @classmethod
    def render(cls, url: str, public_prefix: str, object_key: str) -> str:
        rendered = url.replace(cls.PREFIX, public_prefix.rstrip("/"))
        return rendered.replace(cls.KEY, object_key)


class OpProgress:
    """Отметки прогресса операции: по ним таймаут отличает зависание от работы."""

    def __init__(self) -> None:
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    def idle_sec(self) -> float:
        return time.monotonic() - self._last


class LauncherRead:
    """Процесс чтения из образа: пока он жив, на образе висит его лок.

    Гасится ровно один раз — из finally генератора тела или из close потока,
    смотря что случится раньше. Брошенный поток нужно не только убить, но и
    дочитать его пайпы: wait() у asyncio завершается лишь после того, как
    закрылись все каналы процесса, а в недочитанном stdout остаётся буфер.
    """

    DISCARD_CHUNK: ClassVar[int] = 64 * 1024
    DRAIN_TIMEOUT_SEC: ClassVar[float] = 10.0
    """Мёртвый процесс оставляет в пайпе не больше его ёмкости: этого хватает."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        stdout: asyncio.StreamReader,
        stderr: asyncio.Task[bytes],
    ) -> None:
        self.proc = proc
        self.stdout = stdout
        self.stderr = stderr
        self._released = False

    async def release(self) -> None:
        if self._released:
            return

        self._released = True
        if self.proc.returncode is None:
            self.proc.kill()

        try:
            await asyncio.wait_for(self._drain(), self.DRAIN_TIMEOUT_SEC)
        except TimeoutError:
            # процесс уже получил SIGKILL, значит лок на образе отпущен
            logger.warning(
                "storage: read process pid %s: pipes stayed open %.0fs after kill",
                self.proc.pid,
                self.DRAIN_TIMEOUT_SEC,
            )

    async def _drain(self) -> None:
        while True:
            chunk = await self.stdout.read(self.DISCARD_CHUNK)
            if not chunk:
                break

        await asyncio.gather(self.stderr, return_exceptions=True)
        await self.proc.wait()
