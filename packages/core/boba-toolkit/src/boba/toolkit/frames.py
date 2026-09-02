"""Кадры вызова инструмента: бинарный фрейминг, кодек и гостевая среда ToolIo.

Вызов инструмента всегда потоковый: stdin несёт кадры (первым — config с
injected-конфигом, последним — eos), канал tool_frames несёт кадры тела
наружу. Кадр — конверт границы процесса: JSON-заголовок и сырое тело;
типизацию заголовка держат края (модели инструмента и хоста), транспорт
видит байты. Формат на проводе: [u32 длина заголовка][заголовок JSON][u32
длина тела][тело], числа big-endian.

Ошибки:
FrameProtocolError — поток кадров нарушает контракт: длина за потолком,
    заголовок не разбирается моделью, обрыв внутри кадра, первым кадром
    пришёл не config.
OSError — дескриптор канала закрыт или недоступен; поднимают config,
    inbound и emit.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Iterator, Sequence
from enum import IntEnum, StrEnum
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler, ValidationError
from pydantic_core import CoreSchema, core_schema

from boba.toolkit.stream import Chunk

__all__ = [
    "CallInbox",
    "FrameCodec",
    "FrameHead",
    "FrameKind",
    "FrameLimit",
    "FrameProtocolError",
    "ToolFrame",
    "ToolIo",
]

logger = logging.getLogger(__name__)

HeadModel = TypeVar("HeadModel", bound=BaseModel)


class FrameProtocolError(Exception):
    """Поток кадров нарушает контракт: работать с ним дальше нельзя."""


class FrameKind(StrEnum):
    """Служебные kind'ы кадров; прикладные объявляет инструмент."""

    CONFIG = "config"
    EOS = "eos"


class FrameLimit(IntEnum):
    """Потолки кадра в байтах: контракт обеих сторон границы процесса."""

    HEADER_BYTES = 65_536
    BODY_BYTES = 67_108_864


class FrameHead(BaseModel):
    """Минимальный заголовок кадра: только kind, лишние поля прозрачны."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str


class ToolFrame(BaseModel):
    """Один кадр: JSON-заголовок байтами и сырое тело."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    header: bytes
    body: bytes

    @classmethod
    def of(cls, head: BaseModel, body: bytes = b"") -> ToolFrame:
        header = head.model_dump_json().encode("utf-8")
        return cls(header=header, body=body)

    @classmethod
    def service(cls, kind: FrameKind, body: bytes = b"") -> ToolFrame:
        return cls.of(FrameHead(kind=kind.value), body)

    def header_as(self, model: type[HeadModel]) -> HeadModel:
        try:
            return model.model_validate_json(self.header)
        except ValidationError as exc:
            msg = f"frame header does not match {model.__name__}: {exc}"
            raise FrameProtocolError(msg) from exc

    @property
    def kind(self) -> str:
        return self.header_as(FrameHead).kind


class FrameCodec:
    """Кодек кадров: encode в байты, инкрементальный разбор потока чанков."""

    LEN_BYTES: ClassVar[int] = 4

    def __init__(self, header_limit: int, body_limit: int) -> None:
        if header_limit <= 0:
            msg = f"header_limit must be positive, got {header_limit}"
            raise ValueError(msg)

        if body_limit <= 0:
            msg = f"body_limit must be positive, got {body_limit}"
            raise ValueError(msg)

        self._header_limit = header_limit
        self._body_limit = body_limit
        self._buffer = bytearray()

    def encode(self, frame: ToolFrame) -> bytes:
        if len(frame.header) > self._header_limit:
            msg = (
                f"frame header of {len(frame.header)} bytes exceeds "
                f"{self._header_limit} bytes"
            )
            raise FrameProtocolError(msg)

        if len(frame.body) > self._body_limit:
            msg = (
                f"frame body of {len(frame.body)} bytes exceeds "
                f"{self._body_limit} bytes"
            )
            raise FrameProtocolError(msg)

        header_len = len(frame.header).to_bytes(self.LEN_BYTES, "big")
        body_len = len(frame.body).to_bytes(self.LEN_BYTES, "big")

        return b"".join((header_len, frame.header, body_len, frame.body))

    def feed(self, chunk: Chunk) -> Sequence[ToolFrame]:
        """Принять порцию потока и отдать кадры, собравшиеся целиком."""
        self._buffer.extend(chunk)

        frames: list[ToolFrame] = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames

            frames.append(frame)

    def finish(self) -> None:
        """Конец потока: остаток внутри кадра — обрыв, а не тишина."""
        if not self._buffer:
            return

        msg = f"stream ended inside a frame: {len(self._buffer)} bytes pending"
        raise FrameProtocolError(msg)

    def _next_frame(self) -> ToolFrame | None:
        view = self._buffer
        if len(view) < self.LEN_BYTES:
            return None

        header_len = int.from_bytes(view[: self.LEN_BYTES], "big")
        if header_len > self._header_limit:
            msg = f"frame header of {header_len} bytes exceeds {self._header_limit}"
            raise FrameProtocolError(msg)

        body_len_at = self.LEN_BYTES + header_len
        if len(view) < body_len_at + self.LEN_BYTES:
            return None

        body_len_raw = view[body_len_at : body_len_at + self.LEN_BYTES]
        body_len = int.from_bytes(body_len_raw, "big")
        if body_len > self._body_limit:
            msg = f"frame body of {body_len} bytes exceeds {self._body_limit}"
            raise FrameProtocolError(msg)

        total = body_len_at + self.LEN_BYTES + body_len
        if len(view) < total:
            return None

        header = bytes(view[self.LEN_BYTES : body_len_at])
        body = bytes(view[body_len_at + self.LEN_BYTES : total])
        del view[:total]

        return ToolFrame(header=header, body=body)


class CallInbox:
    """Кадры канала tool_frames: приёмник для насоса, итератор для вызывающего.

    feed зовёт насос в своём потоке, frames читает поток вызывающего;
    итератор кончается вместе с каналом. Ошибка разбора не теряется: она
    поднимается на стороне читателя.
    """

    def __init__(self) -> None:
        self._codec = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)
        self._queue: queue.Queue[ToolFrame | None] = queue.Queue()
        self._failure: Exception | None = None

    def feed(self, chunk: Chunk) -> None:
        """Порция канала; кадры уходят читателю по мере сборки."""
        try:
            frames = self._codec.feed(chunk)
        except FrameProtocolError as exc:
            self.fail(exc)
            return

        for frame in frames:
            self._queue.put(frame)

    def close(self) -> None:
        """Канал кончился: читатель досматривает собранные кадры и выходит."""
        self._queue.put(None)

    def fail(self, error: Exception) -> None:
        """Разбор сорвался: читатель получит эту ошибку после набранных кадров."""
        if self._failure is None:
            self._failure = error

        self._queue.put(None)

    def frames(self) -> Iterator[ToolFrame]:
        while True:
            frame = self._queue.get()
            if frame is None:
                if self._failure is not None:
                    raise self._failure

                return

            yield frame


class ToolIo:
    """Кадровая среда вызова на гостевой стороне.

    Строится гостевым ToolMain на каждый вызов: config отдаёт первый кадр
    stdin с injected-конфигом, inbound — прикладные кадры до eos или EOF,
    emit пишет кадр в канал tool_frames. Тело получает io Injected-параметром,
    только если объявило его в подписи.

    Вне вызова лончером (CLI, `--injected` файлом) среда отвязана: входа нет,
    заголовки emit уходят в лог.
    """

    READ_BYTES: ClassVar[int] = 65536

    def __init__(self, inbound_fd: int, outbound_fd: int) -> None:
        self._inbound_fd = inbound_fd
        self._outbound_fd = outbound_fd
        self._codec_in = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)
        self._codec_out = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)
        self._pending: list[ToolFrame] = []
        self._eof = False
        self._write_lock = threading.Lock()

    @classmethod
    def on_channels(cls, inbound_fd: int, outbound_fd: int) -> ToolIo:
        """Среда вызова лончером: вход и кадры наружу на дескрипторах."""
        return cls(inbound_fd, outbound_fd)

    @classmethod
    def detached(cls) -> ToolIo:
        """Среда без каналов: вход пуст, кадры наружу уходят в лог."""
        return cls(-1, -1)

    @property
    def attached(self) -> bool:
        """Вызов пришёл от лончера: каналы кадров на месте."""
        return self._inbound_fd >= 0

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)

    def config(self) -> bytes:
        """Injected-конфиг первым кадром входа; другой kind — нарушение контракта."""
        frame = self._next()
        if frame is None:
            msg = "call stream ended before the config frame"
            raise FrameProtocolError(msg)

        kind = frame.kind
        if kind != FrameKind.CONFIG:
            msg = f"first frame must be {FrameKind.CONFIG}, got {kind!r}"
            raise FrameProtocolError(msg)

        return frame.body

    def inbound(self) -> Iterator[ToolFrame]:
        """Прикладные кадры входа; eos и EOF завершают итерацию."""
        while True:
            frame = self._next()
            if frame is None:
                return

            if frame.kind == FrameKind.EOS:
                self._eof = True
                return

            yield frame

    def emit(self, head: BaseModel, body: bytes = b"") -> None:
        """Кадр наружу; запись атомарна относительно других потоков тела."""
        frame = ToolFrame.of(head, body)
        data = self._codec_out.encode(frame)

        if self._outbound_fd < 0:
            logger.info(
                "frame emitted (detached): %s, %d body bytes",
                frame.header.decode("utf-8", errors="replace"),
                len(frame.body),
            )
            return

        with self._write_lock:
            self._write_all(self._outbound_fd, data)

    def _next(self) -> ToolFrame | None:
        """Следующий кадр входа; None — поток кончился."""
        if self._pending:
            return self._pending.pop(0)

        if self._eof:
            return None

        if self._inbound_fd < 0:
            self._eof = True
            return None

        while not self._pending:
            chunk = os.read(self._inbound_fd, self.READ_BYTES)
            if not chunk:
                self._eof = True
                self._codec_in.finish()
                return None

            self._pending.extend(self._codec_in.feed(chunk))

        return self._pending.pop(0)

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)

        while view.nbytes:
            written = os.write(fd, view)
            view = view[written:]
