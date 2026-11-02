"""Модели и инкрементальный кодек кадров инструмента без файловых дескрипторов.

Ошибки:
FrameProtocolError — поток кадров нарушает контракт.
ValueError — задан неположительный предел размера кадра.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator, Sequence
from enum import IntEnum
from typing import ClassVar, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from boba.toolkit.stream import Chunk

__all__ = [
    "FrameCodec",
    "FrameHead",
    "FrameLimit",
    "FrameProtocolError",
    "FrameTransport",
    "ToolFrame",
]

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

    def encode_parts(
        self, header: bytes, body: Chunk
    ) -> tuple[bytes, bytes, bytes, Chunk]:
        """Части кадра в порядке провода для раздельной записи (writev): длина
        заголовка, заголовок, длина тела, тело. Ничего не склеивается —
        заголовок и тело уходят в запись теми же объектами."""
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

        return (
            len(header).to_bytes(self.LEN_BYTES, "big"),
            header,
            len(body).to_bytes(self.LEN_BYTES, "big"),
            body,
        )

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


class FrameTransport(Protocol):
    """Транспорт портов инструмента; реализацию получает сборка вызова.

    Порты валидируют модели и работают с байтами через этот контракт.
    ToolIo реализует его файловыми дескрипторами в boba.tool_io.frames.
    """

    @abstractmethod
    def inbound_capacity(self) -> int: ...

    @abstractmethod
    def outbound_capacity(self) -> int: ...

    @abstractmethod
    def read_frames(self) -> Iterator[tuple[bytes, memoryview]]: ...

    @abstractmethod
    def emit(self, head: BaseModel, body: Chunk = b"") -> None: ...

    @abstractmethod
    def read_into(self, buffer: memoryview) -> int: ...

    @abstractmethod
    def write_chunk(self, chunk: memoryview) -> None: ...
