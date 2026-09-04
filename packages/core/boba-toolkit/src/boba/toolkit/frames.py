"""Кадры данных вызова инструмента: формат на проводе, кодек и среда ToolIo.

Потоковый инструмент получает данные порциями и отдаёт результаты
порциями, не дожидаясь конца вызова (аудио с микрофона внутрь, гипотезы
распознавания наружу). Порция называется кадром: JSON-заголовок с
метаданными плюс сырое тело (байты аудио, файла, текста). Формат на
проводе: [u32 длина заголовка][заголовок][u32 длина тела][тело],
big-endian. Транспорт видит только байты; какие модели лежат в заголовках,
знают инструмент и его вызывающий.

Кадры ходят по двум пайпам вызова: stdin несёт кадры входа до EOF, канал
tool_frames — кадры тела наружу. Служебных кадров нет: конфиг едет
отдельным каналом --injected-fd, конец входа — EOF пайпа. Хост пишет вход
через FrameInput и читает выход через CallInbox (boba.toolkit.pump); тело
инструмента работает с типизированными портами (boba.toolkit.ports), под
которыми транспортом лежит ToolIo.

Ошибки:
FrameProtocolError — поток кадров нарушает контракт: длина за потолком,
    заголовок не разбирается моделью, обрыв внутри кадра.
OSError — дескриптор канала закрыт или недоступен; поднимают inbound и emit.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Iterator, Sequence
from enum import IntEnum
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler, ValidationError
from pydantic_core import CoreSchema, core_schema

from boba.toolkit.stream import Chunk

__all__ = [
    "CallInbox",
    "FrameCodec",
    "FrameHead",
    "FrameLimit",
    "FrameProtocolError",
    "ToolFrame",
    "ToolIo",
]

logger = logging.getLogger(__name__)

HeadModel = TypeVar("HeadModel", bound=BaseModel)


class FrameProtocolError(Exception):
    """Поток кадров нарушает контракт: работать с ним дальше нельзя."""


class FrameLimit(IntEnum):
    """Потолки размера кадра в байтах, одинаковые на обеих сторонах границы
    процесса.

    Ограничивают память на сборку одного кадра и отсекают битый поток, в
    котором поле длины — мусор.
    """

    HEADER_BYTES = 65_536
    BODY_BYTES = 67_108_864


class FrameHead(BaseModel):
    """Минимальный заголовок кадра — только поле kind (вид данных).

    Нужен, чтобы прочитать kind кадра, не зная прикладной модели заголовка:
    прикладные заголовки объявляет сам инструмент, лишние поля здесь
    игнорируются.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str


class ToolFrame(BaseModel):
    """Один кадр: JSON-заголовок сырыми байтами и тело.

    Заголовок хранится байтами, а не моделью, потому что транспорт
    прикладных моделей не знает — типизация живёт на краях: of()
    упаковывает модель в заголовок, header_as() разбирает его обратно.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    header: bytes
    body: bytes

    @classmethod
    def of(cls, head: BaseModel, body: bytes = b"") -> ToolFrame:
        header = head.model_dump_json().encode("utf-8")
        return cls(header=header, body=body)

    def header_as(self, model: type[HeadModel]) -> HeadModel:
        try:
            return model.model_validate_json(self.header)
        except ValidationError as exc:
            shown = self.header[:200]
            msg = f"frame header {shown!r} does not match {model.__name__}: {exc}"
            raise FrameProtocolError(msg) from exc

    @property
    def kind(self) -> str:
        return self.header_as(FrameHead).kind


class FrameCodec:
    """Кодек кадров: encode собирает кадр в байты, feed инкрементально
    разбирает поток порций.

    Кадр может приходить хоть по байту, поэтому кодек хранит недособранный
    хвост — свой экземпляр на каждый поток. finish() зовётся на EOF: если
    в буфере остался кусок кадра, это обрыв, а не тишина.
    """

    LEN_BYTES: ClassVar[int] = 4

    def __init__(self, header_limit: int, body_limit: int) -> None:
        if header_limit <= 0:
            msg = f"FrameCodec header_limit must be positive, got {header_limit}"
            raise ValueError(msg)

        if body_limit <= 0:
            msg = f"FrameCodec body_limit must be positive, got {body_limit}"
            raise ValueError(msg)

        self._header_limit = header_limit
        self._body_limit = body_limit
        self._buffer = bytearray()

    def encode_parts(self, header: bytes, body: Chunk) -> tuple[bytes, Chunk]:
        """Префикс кадра и тело для раздельной записи (writev): тело не
        копируется — оно уходит в запись тем же объектом."""
        if len(header) > self._header_limit:
            msg = (
                f"outbound frame header of {len(header)} bytes exceeds "
                f"the limit of {self._header_limit} bytes"
            )
            raise FrameProtocolError(msg)

        if len(body) > self._body_limit:
            msg = (
                f"outbound frame body of {len(body)} bytes exceeds "
                f"the limit of {self._body_limit} bytes"
            )
            raise FrameProtocolError(msg)

        prefix = b"".join(
            (
                len(header).to_bytes(self.LEN_BYTES, "big"),
                header,
                len(body).to_bytes(self.LEN_BYTES, "big"),
            )
        )

        return prefix, body

    def encode(self, frame: ToolFrame) -> bytes:
        prefix, body = self.encode_parts(frame.header, frame.body)

        return prefix + bytes(body)

    def feed(self, chunk: Chunk) -> Sequence[ToolFrame]:
        """Принять порцию потока и отдать кадры, собравшиеся целиком.

        Быстрый путь: при пустом буфере кадры вырезаются прямо из порции,
        без копии в накопительный буфер; в буфер уходит только неполный
        хвост.
        """
        frames: list[ToolFrame] = []

        offset = 0
        if not self._buffer:
            view = memoryview(chunk)
            while True:
                parsed = self._parse_at(view, offset)
                if parsed is None:
                    break

                frame, offset = parsed
                frames.append(frame)

            self._buffer.extend(view[offset:])
            return frames

        self._buffer.extend(chunk)

        while True:
            frame = self._next_frame()
            if frame is None:
                return frames

            frames.append(frame)

    def finish(self) -> None:
        """Конец потока: остаток внутри кадра — обрыв, а не тишина."""
        if not self._buffer:
            return

        msg = (
            f"frame stream ended inside a frame: {len(self._buffer)} bytes "
            "of an unfinished frame are pending at EOF"
        )
        raise FrameProtocolError(msg)

    def _next_frame(self) -> ToolFrame | None:
        view = memoryview(self._buffer)
        parsed = self._parse_at(view, 0)
        view.release()

        if parsed is None:
            return None

        frame, consumed = parsed
        del self._buffer[:consumed]

        return frame

    def _parse_at(self, view: memoryview, offset: int) -> tuple[ToolFrame, int] | None:
        """Кадр по смещению view; None — данных на целый кадр не хватает."""
        remaining = len(view) - offset
        if remaining < self.LEN_BYTES:
            return None

        header_len = int.from_bytes(view[offset : offset + self.LEN_BYTES], "big")
        if header_len > self._header_limit:
            msg = (
                f"inbound frame header of {header_len} bytes exceeds "
                f"the limit of {self._header_limit} bytes"
            )
            raise FrameProtocolError(msg)

        body_len_at = offset + self.LEN_BYTES + header_len
        if len(view) < body_len_at + self.LEN_BYTES:
            return None

        body_len_raw = view[body_len_at : body_len_at + self.LEN_BYTES]
        body_len = int.from_bytes(body_len_raw, "big")
        if body_len > self._body_limit:
            msg = (
                f"inbound frame body of {body_len} bytes exceeds "
                f"the limit of {self._body_limit} bytes"
            )
            raise FrameProtocolError(msg)

        total = body_len_at + self.LEN_BYTES + body_len
        if len(view) < total:
            return None

        header = bytes(view[offset + self.LEN_BYTES : body_len_at])
        body = bytes(view[body_len_at + self.LEN_BYTES : total])

        return ToolFrame(header=header, body=body), total


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


class ToolIo:
    """Транспорт каналов данных на стороне тела инструмента.

    Кадровый режим: inbound() читает кадры входа со stdin до EOF, emit()
    пишет кадр наружу в канал tool_frames. Сырой режим — для RawInbound и
    RawOutbound: read_chunks() отдаёт порции байтов как есть, write_chunk()
    пишет порцию без какого-либо кадрирования — на проводе только сами
    данные. Режим канала выбирает декларация порта в подписи тела, транспорт
    одинаково умеет оба.

    Телам инструментов наружу не отдаётся: они объявляют типизированные
    порты (boba.toolkit.ports), а ToolMain строит ToolIo из номеров
    дескрипторов команды и подкладывает его портам транспортом.

    При запуске человеком (--injected файлом, без каналов лончера) среда
    отвязана: вход пуст, записи наружу уходят в лог.
    """

    READ_BYTES: ClassVar[int] = 65536
    LEN_PREFIX: ClassVar[int] = FrameCodec.LEN_BYTES

    def __init__(self, inbound_fd: int, outbound_fd: int) -> None:
        self._inbound_fd = inbound_fd
        self._outbound_fd = outbound_fd
        self._codec_out = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)
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
        prefix, body = self._codec_out.encode_parts(header, body)

        if self._outbound_fd < 0:
            logger.info(
                "frame emitted (detached): %s, %d body bytes",
                header.decode("utf-8", errors="replace"),
                len(body),
            )
            return

        with self._write_lock:
            self._writev_all(self._outbound_fd, prefix, body)

    def read_chunks(self) -> Iterator[bytes]:
        """Сырой вход: порции байтов как есть, EOF пайпа завершает итерацию."""
        if self._inbound_fd < 0:
            return

        while True:
            chunk = os.read(self._inbound_fd, self.READ_BYTES)
            if not chunk:
                return

            yield chunk

    def write_chunk(self, chunk: Chunk) -> None:
        """Сырой выход: порция пишется без кадрирования, атомарно к другим
        потокам тела."""
        if self._outbound_fd < 0:
            logger.info("raw chunk emitted (detached): %d bytes", len(chunk))
            return

        with self._write_lock:
            self._writev_all(self._outbound_fd, b"", chunk)

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

    @staticmethod
    def _writev_all(fd: int, first: bytes, second: Chunk) -> None:
        """Записать обе части целиком; тело не склеивается с префиксом."""
        parts: list[memoryview] = []

        head = memoryview(first)
        if head.nbytes:
            parts.append(head)

        tail = memoryview(second)
        if tail.nbytes:
            parts.append(tail)

        while parts:
            written = os.writev(fd, parts)

            while written and parts:
                head = parts[0]
                if written >= head.nbytes:
                    written -= head.nbytes
                    parts.pop(0)
                    continue

                parts[0] = head[written:]
                written = 0
