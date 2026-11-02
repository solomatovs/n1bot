"""Передача кадров и сырых байтов инструмента через файловые дескрипторы.

Ошибки:
FrameProtocolError — поток кадров или канал нарушает контракт.
OSError — дескриптор канала закрыт или недоступен.
"""

from __future__ import annotations

import fcntl
import logging
import os
import queue
import threading
from collections.abc import Iterator, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from boba.toolkit.frames import (
    FrameCodec,
    FrameLimit,
    FrameProtocolError,
    FrameTransport,
    ToolFrame,
)
from boba.toolkit.stream import Chunk

__all__ = ["CallInbox", "PartsWriter", "ToolIo"]

logger = logging.getLogger(__name__)


class PartsWriter:
    """Запись нескольких буферов в дескриптор одним writev без склейки.

    Общая точка записи обеих сторон пайпа: тело пишет ею кадры и сырые
    порции наружу (ToolIo), хост — входы тела (CallInput). Обычно ядро
    принимает всё за один вызов, и части уходят теми же объектами, что
    пришли. Если запись прервана сигналом и ядро приняло только начало,
    остаток дописывается через memoryview — срез view не копирует данные.

    Ошибки:
    OSError — дескриптор закрыт либо читатель закрыл свой конец пайпа.
    """

    def write(self, fd: int, parts: Sequence[Chunk]) -> None:
        total = 0
        for part in parts:
            total += len(part)

        if total == 0:
            return

        written = os.writev(fd, parts)
        if written == total:
            return

        self._write_rest(fd, parts, written)

    def _write_rest(self, fd: int, parts: Sequence[Chunk], written: int) -> None:
        pending = list(self._rest(parts, written))

        while pending:
            written = os.writev(fd, pending)
            pending = list(self._rest(pending, written))

    @staticmethod
    def _rest(parts: Sequence[Chunk], skip: int) -> Iterator[memoryview]:
        """Части после первых skip байт: ушедшие целиком пропускаются, начатая
        отдаётся с места обрыва."""
        for part in parts:
            view = memoryview(part)
            if skip >= view.nbytes:
                skip -= view.nbytes
                continue

            yield view[skip:]
            skip = 0


class CallInbox:
    """Мост кадров между потоком насоса и читателем вызова на хосте.

    Насос (ChannelPump) складывает сюда порции канала tool_frames из
    своего потока; вызывающий читает собранные кадры итератором frames()
    из своего. Итератор кончается вместе с каналом, ошибка разбора не
    глотается — читатель получит её после уже собранных кадров. Читатель
    один: за этим следит PumpedCall.
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


class ToolIo(FrameTransport):
    """Транспорт каналов данных на стороне тела инструмента.

    Кадровый режим: read_frames() читает кадры своего входа до EOF, emit()
    пишет кадр наружу в канал tool_frames. У каждого входного порта свой
    ToolIo на своём пайпе, у выходного — на канале кадров. Сырой режим —
    для RawInbound и RawOutbound: read_into() кладёт очередные байты в буфер как есть,
    write_chunk() пишет порцию без какого-либо кадрирования — на проводе
    только сами данные. Режим канала выбирает декларация порта в подписи тела, транспорт
    одинаково умеет оба.

    Телам инструментов наружу не отдаётся: они объявляют типизированные
    порты (boba.toolkit.ports), а ToolMain строит ToolIo из номеров
    дескрипторов команды и подкладывает его портам транспортом.

    При запуске человеком (--injected файлом, без каналов лончера) среда
    отвязана: вход пуст, записи наружу уходят в лог.
    """

    LEN_PREFIX: ClassVar[int] = FrameCodec.LEN_BYTES

    DETACHED_BYTES: ClassVar[int] = 262144
    """Порция обмена, когда канала нет (запуск человеком): спросить ёмкость
    не у кого."""

    def __init__(self, inbound_fd: int, outbound_fd: int) -> None:
        self._inbound_fd = inbound_fd
        self._outbound_fd = outbound_fd
        self._codec_out = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)
        self._write_lock = threading.Lock()
        self._writer = PartsWriter()

    @property
    def attached(self) -> bool:
        """Вызов пришёл от лончера: каналы кадров на месте."""
        return self._inbound_fd >= 0

    def inbound_capacity(self) -> int:
        """Ёмкость входного канала в байтах: её назначил тот, кто строил
        канал (pipe_bytes пишущего вызова), и тело читает порциями этого
        размера."""
        return self._capacity(self._inbound_fd)

    def outbound_capacity(self) -> int:
        """Ёмкость выходного канала в байтах: тело пишет порциями этого
        размера — порция крупнее не уходит в канал за одну запись, мельче —
        лишние системные вызовы."""
        return self._capacity(self._outbound_fd)

    def _capacity(self, fd: int) -> int:
        if fd < 0:
            return self.DETACHED_BYTES

        try:
            return fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
        except OSError as exc:
            msg = (
                f"channel fd {fd}: expected a pipe to read its capacity from, got {exc}"
            )
            raise FrameProtocolError(msg) from exc

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(cls)

    def read_frames(self) -> Iterator[tuple[bytes, memoryview]]:
        """Кадры входа точным чтением по границам: заголовок байтами и тело
        отдельным view.

        Тело читается readinto в собственный буфер нужного размера — одна
        копия из ядра, без накопительного буфера и вырезок. View владеет
        своим буфером: его можно держать сколько угодно. EOF на границе
        кадров завершает итерацию, обрыв посреди кадра — FrameProtocolError.
        """
        if self._inbound_fd < 0:
            return

        while True:
            head_len_raw = self._read_exact(self.LEN_PREFIX, at_boundary=True)
            if head_len_raw is None:
                return

            header_len = int.from_bytes(head_len_raw, "big")
            if header_len > FrameLimit.HEADER_BYTES:
                msg = (
                    f"inbound fd {self._inbound_fd}: frame header of "
                    f"{header_len} bytes exceeds the limit of "
                    f"{FrameLimit.HEADER_BYTES} bytes"
                )
                raise FrameProtocolError(msg)

            header = self._require(self._read_exact(header_len, at_boundary=False))

            body_len_raw = self._require(
                self._read_exact(self.LEN_PREFIX, at_boundary=False)
            )
            body_len = int.from_bytes(body_len_raw, "big")
            if body_len > FrameLimit.BODY_BYTES:
                msg = (
                    f"inbound fd {self._inbound_fd}: frame body of "
                    f"{body_len} bytes exceeds the limit of "
                    f"{FrameLimit.BODY_BYTES} bytes"
                )
                raise FrameProtocolError(msg)

            body = bytearray(body_len)
            self._readinto_exact(memoryview(body))

            yield header, memoryview(body)

    def emit(self, head: BaseModel, body: Chunk = b"") -> None:
        """Кадр наружу; тело пишется writev без копии, запись атомарна
        относительно других потоков тела."""
        header = head.model_dump_json().encode("utf-8")
        parts = self._codec_out.encode_parts(header, body)

        if self._outbound_fd < 0:
            logger.info(
                "frame emitted (detached): %s, %d body bytes",
                header.decode("utf-8", errors="replace"),
                len(body),
            )
            return

        with self._write_lock:
            self._writer.write(self._outbound_fd, parts)

    def read_into(self, buffer: memoryview) -> int:
        """Сырой вход: очередные байты в buffer как есть, не больше его длины;
        0 — EOF пайпа. Чтение блокирующее."""
        if self._inbound_fd < 0:
            return 0

        return os.readv(self._inbound_fd, [buffer])

    def write_chunk(self, chunk: memoryview) -> None:
        """Сырой выход: порция пишется без кадрирования, атомарно к другим
        потокам тела."""
        if self._outbound_fd < 0:
            logger.info("raw chunk emitted (detached): %d bytes", len(chunk))
            return

        with self._write_lock:
            self._writer.write(self._outbound_fd, (chunk,))

    def _read_exact(self, count: int, *, at_boundary: bool) -> bytes | None:
        """Ровно count байт входа; None — чистый EOF на границе кадров.

        EOF посреди начатого кадра (at_boundary=False либо часть уже
        прочитана) — обрыв потока, FrameProtocolError.
        """
        collected = bytearray()

        while len(collected) < count:
            chunk = os.read(self._inbound_fd, count - len(collected))
            if not chunk:
                if at_boundary and not collected:
                    return None

                msg = (
                    f"inbound fd {self._inbound_fd}: stream ended inside a "
                    f"frame after {len(collected)} of {count} expected bytes"
                )
                raise FrameProtocolError(msg)

            collected.extend(chunk)

        return bytes(collected)

    @staticmethod
    def _require(data: bytes | None) -> bytes:
        if data is None:
            msg = (
                "inbound frame stream ended inside a frame: the header or "
                "body length prefix is missing"
            )
            raise FrameProtocolError(msg)

        return data

    def _readinto_exact(self, target: memoryview) -> None:
        """Заполнить буфер тела целиком; EOF раньше — обрыв потока."""
        filled = 0

        while filled < target.nbytes:
            got = os.readv(self._inbound_fd, [target[filled:]])
            if got == 0:
                msg = (
                    f"inbound fd {self._inbound_fd}: stream ended inside a "
                    f"frame body after {filled} of {target.nbytes} expected bytes"
                )
                raise FrameProtocolError(msg)

            filled += got
