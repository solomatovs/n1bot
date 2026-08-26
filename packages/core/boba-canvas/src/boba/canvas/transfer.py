"""Передача файлов: заголовки, формат и прогресс передачи, политика загрузки.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from email.message import Message
from enum import StrEnum
from typing import ClassVar

__all__ = [
    "ContentDisposition",
    "FileHeader",
    "TransferFormat",
    "TransferProgress",
    "UploadPolicy",
]


class FileHeader(StrEnum):
    """HTTP-заголовки отдачи файла."""

    CONTENT_LENGTH = "Content-Length"
    CONTENT_RANGE = "Content-Range"
    CONTENT_DISPOSITION = "Content-Disposition"
    ACCEPT_RANGES = "Accept-Ranges"
    RANGE = "Range"


class ContentDisposition:
    """Значение Content-Disposition: имя по RFC 2231, не-ASCII — через filename*."""

    INLINE: ClassVar[str] = "inline"

    @classmethod
    def inline(cls, name: str) -> str:
        message = Message()
        message.add_header(FileHeader.CONTENT_DISPOSITION, cls.INLINE, filename=name)

        return str(message[FileHeader.CONTENT_DISPOSITION])


@dataclass(frozen=True, slots=True)
class TransferFormat:
    """Человекочитаемые объём, время и скорость передачи."""

    mib: int

    def volume(self, size: int) -> str:
        return f"{size / self.mib:.1f} MiB"

    @staticmethod
    def took(started: float) -> str:
        return f"{time.monotonic() - started:.1f}s"

    def rate(self, size: int, started: float) -> str:
        elapsed = time.monotonic() - started
        if elapsed <= 0:
            return "instant"

        return f"{size / self.mib / elapsed:.1f} MiB/s"

    @staticmethod
    def share(done: int, total: int) -> str:
        if total <= 0:
            return "unknown"

        return f"{done * 100 // total}%"


class TransferProgress:
    """Ход передачи: считает байты и подсказывает, когда писать отметку в лог."""

    def __init__(self, fmt: TransferFormat, every_bytes: int) -> None:
        self._fmt = fmt
        self._every = every_bytes
        self._started = time.monotonic()
        self._done = 0
        self._milestone = every_bytes

    @property
    def done(self) -> int:
        return self._done

    def advance(self, size: int) -> bool:
        """Учитывает чанк; True — пройден очередной рубеж для отметки."""
        self._done += size

        if self._done < self._milestone:
            return False

        self._milestone = self._done + self._every
        return True

    def volume(self) -> str:
        return self._fmt.volume(self._done)

    def took(self) -> str:
        return self._fmt.took(self._started)

    def rate(self) -> str:
        return self._fmt.rate(self._done, self._started)

    def share(self, total: int) -> str:
        return self._fmt.share(self._done, total)


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """Маршруты и пределы потоковой передачи вложений в обе стороны."""

    upload_path: str = "/project/file"
    download_path: str = "/project/file/{file_id}"
    no_space_status: int = 507
    mib: int = 1024 * 1024
    log_every_bytes: int = 32 * 1024 * 1024
    """Шаг отметок при заливке: тело идёт от клиента, счёт на десятки МиБ."""
    serve_log_every_bytes: int = 4 * 1024 * 1024
    """Шаг отметок при отдаче: вложения мельче, шаг заливки их не показал бы."""
    drain_bytes: int = 8 * 1024 * 1024
    """Потолок вычитывания после отказа: дальше соединение закрывается."""
    drain_seconds: float = 3.0
