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
            msg = f"frame header does not match {model.__name__}: {exc}"
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

    def inbound(self) -> Iterator[ToolFrame]:
        """Прикладные кадры входа; EOF пайпа завершает итерацию."""
        while True:
            frame = self._next()
            if frame is None:
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

    def read_chunks(self) -> Iterator[bytes]:
        """Сырой вход: порции байтов как есть, EOF пайпа завершает итерацию."""
        if self._inbound_fd < 0:
            return

        while True:
            chunk = os.read(self._inbound_fd, self.READ_BYTES)
            if not chunk:
                return

            yield chunk

    def write_chunk(self, chunk: bytes) -> None:
        """Сырой выход: порция пишется без кадрирования, атомарно к другим
        потокам тела."""
        if self._outbound_fd < 0:
            logger.info("raw chunk emitted (detached): %d bytes", len(chunk))
            return

        with self._write_lock:
            self._write_all(self._outbound_fd, chunk)

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
