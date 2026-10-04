"""Отдача файла хранилища по HTTP потоком: Content-Length, Range, журнал хода.

Тело идёт окном чанков из хранилища и в памяти целиком не поднимается;
заголовок Range отвечает 206. Модулем пользуются входы, отдающие файлы
workspace: маршруты вложений чата и маршруты файлов сервиса boba-mcp.

Ошибки:
HTTPException 404 — объекта нет в хранилище.
HTTPException 416 — диапазон начинается за концом файла.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse

from boba.canvas.storage import OpenedStream, StorageNotFoundError
from boba.canvas.transfer import (
    FileHeader,
    TransferFormat,
    TransferProgress,
    UploadPolicy,
)
from boba.runtime.storage import StorageClient
from boba.workspace.launcher import ReadWindow

__all__ = ["ByteRange", "RangeHeader", "StreamedFile", "SuffixRange"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Диапазон bytes=start-end: end включительно, None — до конца файла."""

    start: int
    end: int | None

    def window(self) -> ReadWindow:
        if self.end is None:
            return ReadWindow(offset=self.start, length=None)

        return ReadWindow(offset=self.start, length=self.end - self.start + 1)


@dataclass(frozen=True, slots=True)
class SuffixRange:
    """Диапазон bytes=-N: последние N байт, окно зависит от размера файла."""

    length: int

    def window(self, size: int) -> ReadWindow:
        offset = max(size - self.length, 0)
        return ReadWindow(offset=offset, length=None)


class RangeHeader:
    """Разбор заголовка Range: только одиночный диапазон байтов."""

    UNIT: ClassVar[str] = "bytes="

    @classmethod
    def parse(cls, header: str) -> ByteRange | SuffixRange | None:
        """None — заголовок не разобран: RFC 9110 позволяет его игнорировать."""
        value = header.strip()
        if not value.startswith(cls.UNIT):
            return None

        spec = value[len(cls.UNIT) :].strip()
        if "," in spec:
            return None

        start_text, sep, end_text = spec.partition("-")
        if not sep:
            return None

        start_text = start_text.strip()
        end_text = end_text.strip()

        if not start_text:
            return cls._suffix(end_text)

        return cls._bounded(start_text, end_text)

    @staticmethod
    def _suffix(end_text: str) -> SuffixRange | None:
        if not end_text.isdigit():
            return None

        length = int(end_text)
        if length == 0:
            return None

        return SuffixRange(length=length)

    @staticmethod
    def _bounded(start_text: str, end_text: str) -> ByteRange | None:
        if not start_text.isdigit():
            return None

        start = int(start_text)
        if not end_text:
            return ByteRange(start=start, end=None)

        if not end_text.isdigit():
            return None

        end = int(end_text)
        if end < start:
            return None

        return ByteRange(start=start, end=end)


class StreamedFile:
    """HTTP-ответ телом объекта хранилища: стрим чанков, Content-Length, Range.

    Ход отдачи пишется в лог: строка перед первым байтом, отметки по мере
    передачи и итог. Обрыв клиента — обычное дело (перемотка видео, закрытая
    вкладка), поэтому недоотданное тело отмечается отдельно.
    """

    ACCEPT_RANGES: ClassVar[str] = "bytes"

    def __init__(self, storage: StorageClient, policy: UploadPolicy) -> None:
        self._storage = storage
        self._policy = policy
        self._format = TransferFormat(policy.mib)

    async def respond(
        self,
        object_key: str,
        *,
        mime: str,
        range_header: str,
        content_disposition: str,
    ) -> Response:
        """Ответ 200/206/416; нет объекта — HTTPException 404 до первого байта."""
        try:
            return await self._respond(
                object_key, mime, range_header, content_disposition
            )
        except StorageNotFoundError as e:
            msg = f"file not found in the storage: {object_key}: {e}"
            raise HTTPException(status_code=404, detail=msg) from e

    async def _respond(
        self,
        object_key: str,
        mime: str,
        range_header: str,
        content_disposition: str,
    ) -> Response:
        parsed = None
        if range_header:
            parsed = RangeHeader.parse(range_header)

        if isinstance(parsed, SuffixRange):
            stat = await self._storage.stat(object_key)
            window = parsed.window(stat.size)
            return await self._ranged(object_key, window, mime, content_disposition)

        if isinstance(parsed, ByteRange):
            window = parsed.window()
            return await self._ranged(object_key, window, mime, content_disposition)

        opened = await self._storage.open_stream(object_key, ReadWindow.entire())
        return self._full(object_key, opened, mime, content_disposition)

    async def _ranged(
        self,
        object_key: str,
        window: ReadWindow,
        mime: str,
        content_disposition: str,
    ) -> Response:
        opened = await self._storage.open_stream(object_key, window)

        if window.offset >= opened.stat.size:
            await opened.close()
            headers: dict[str, str] = {
                FileHeader.CONTENT_RANGE: f"bytes */{opened.stat.size}",
            }
            msg = (
                f"range starts at {window.offset}, beyond the size "
                f"{opened.stat.size} of {object_key}"
            )
            raise HTTPException(status_code=416, detail=msg, headers=headers)

        return self._partial(object_key, opened, window, mime, content_disposition)

    def _full(
        self,
        object_key: str,
        opened: OpenedStream,
        mime: str,
        content_disposition: str,
    ) -> Response:
        headers: dict[str, str] = {
            FileHeader.CONTENT_LENGTH: str(opened.stat.size),
            FileHeader.ACCEPT_RANGES: self.ACCEPT_RANGES,
        }
        if content_disposition:
            headers[FileHeader.CONTENT_DISPOSITION] = content_disposition

        body = self._logged(object_key, opened, opened.stat.size, "whole file")
        return StreamingResponse(body, media_type=mime, headers=headers)

    def _partial(
        self,
        object_key: str,
        opened: OpenedStream,
        window: ReadWindow,
        mime: str,
        content_disposition: str,
    ) -> Response:
        size = opened.stat.size
        length = window.resolve_length(size)
        last = window.offset + length - 1

        headers: dict[str, str] = {
            FileHeader.CONTENT_LENGTH: str(length),
            FileHeader.CONTENT_RANGE: f"bytes {window.offset}-{last}/{size}",
            FileHeader.ACCEPT_RANGES: self.ACCEPT_RANGES,
        }
        if content_disposition:
            headers[FileHeader.CONTENT_DISPOSITION] = content_disposition

        window_label = f"bytes {window.offset}-{last} of {size}"
        body = self._logged(object_key, opened, length, window_label)
        return StreamingResponse(
            body, status_code=206, media_type=mime, headers=headers
        )

    async def _logged(
        self,
        object_key: str,
        opened: OpenedStream,
        total: int,
        window_label: str,
    ) -> AsyncIterator[bytes]:
        """Отдаёт тело, отмечая в логе начало, ход и итог передачи."""
        progress = TransferProgress(self._format, self._policy.serve_log_every_bytes)
        logger.info(
            "serving: %s sending %s (%s)",
            object_key,
            self._format.volume(total),
            window_label,
        )

        complete = False
        try:
            async for chunk in opened.chunks:
                if progress.advance(len(chunk)):
                    logger.info(
                        "serving: %s streaming, %s of %s (%s, %s)",
                        object_key,
                        progress.volume(),
                        self._format.volume(total),
                        progress.share(total),
                        progress.rate(),
                    )

                yield chunk

            complete = True
        finally:
            # клиент, дочитавший тело, закрывает соединение, пока генератор стоит
            # на последнем yield: отданное целиком окно — это завершённая отдача
            if progress.done >= total:
                complete = True

            self._finished(object_key, progress, total, complete=complete)

    def _finished(
        self,
        object_key: str,
        progress: TransferProgress,
        total: int,
        *,
        complete: bool,
    ) -> None:
        if complete:
            logger.info(
                "serving: %s sent, %s in %s (%s)",
                object_key,
                progress.volume(),
                progress.took(),
                progress.rate(),
            )
            return

        logger.warning(
            "serving: %s aborted by the client, %s of %s sent (%s) in %s",
            object_key,
            progress.volume(),
            self._format.volume(total),
            progress.share(total),
            progress.took(),
        )
