"""Растущий байтовый буфер для чтения строк из файла."""

from __future__ import annotations

from collections.abc import Iterator
from io import BufferedReader


class GrowBuffer:
    """Байтовый буфер, который растёт но никогда не сжимается."""

    _INITIAL_CAPACITY = 4096

    def __init__(self, fd: BufferedReader, *, max_capacity: int | None = None) -> None:
        """Создать буфер поверх fd; max_capacity — верхняя граница ёмкости."""
        if max_capacity is not None and max_capacity <= 0:
            raise ValueError(f"max_capacity must be positive, got {max_capacity}")

        self._fd = fd
        self._max_capacity = max_capacity
        initial = self._INITIAL_CAPACITY
        if max_capacity is not None:
            initial = min(initial, max_capacity)
        self._buf = bytearray(initial)
        self._size = 0
        self._consumed = 0

    @property
    def capacity(self) -> int:
        """Текущая ёмкость буфера."""
        return len(self._buf)

    @property
    def max_capacity(self) -> int | None:
        """Верхняя граница ёмкости."""
        return self._max_capacity

    @property
    def consumed(self) -> int:
        """Количество байт, обработанных последним iter_lines_*()."""
        return self._consumed

    def tail(self) -> memoryview:
        """Непотреблённый хвост буфера — байты после последнего separator."""
        return memoryview(self._buf)[self._consumed : self._size]

    def iter_lines_forward(
        self, separator: bytes, offset: int = 0
    ) -> Iterator[memoryview]:
        """Yield полные строки [offset, EOF] от начала к концу."""
        end = self._load(separator, offset)
        if end == 0:
            return

        sep_len = len(separator)
        start = 0
        while start < end:
            pos = self._buf.find(separator, start, end)
            if pos == -1:
                break
            yield memoryview(self._buf)[start:pos]
            start = pos + sep_len

    def iter_lines_backward(
        self, separator: bytes, offset: int = 0
    ) -> Iterator[memoryview]:
        """Yield полные строки [offset, EOF] в обратном порядке."""
        end = self._load(separator, offset)
        if end == 0:
            return

        sep_len = len(separator)
        line_end = end - sep_len
        while True:
            prev_sep = self._buf.rfind(separator, 0, line_end)
            if prev_sep == -1:
                yield memoryview(self._buf)[0:line_end]
                return
            line_start = prev_sep + sep_len
            yield memoryview(self._buf)[line_start:line_end]
            line_end = prev_sep

    def _load(self, separator: bytes, offset: int) -> int:
        """Прочитать [offset, EOF] и вернуть позицию после последнего separator."""
        self._fd.seek(offset)
        self._fill()

        if self._size == 0:
            self._consumed = 0
            return 0

        last_sep = self._buf.rfind(separator, 0, self._size)
        if last_sep == -1:
            self._consumed = 0
            return 0

        end = last_sep + len(separator)
        self._consumed = end
        return end

    def _fill(self) -> None:
        """Прочитать данные из fd в буфер."""
        self._size = 0
        while True:
            if self._size == len(self._buf):
                self._grow()
            n = self._fd.readinto(memoryview(self._buf)[self._size :])
            if not n:
                break
            self._size += n

    def _grow(self) -> None:
        """Удвоить ёмкость in-place; BufferError при достижении max_capacity или живых memoryview."""
        cur = len(self._buf)
        target = cur * 2
        if self._max_capacity is not None:
            target = min(target, self._max_capacity)

        grow_by = target - cur
        if grow_by <= 0:
            raise BufferError(
                f"GrowBuffer: capacity {cur} reached max_capacity "
                f"{self._max_capacity} — input exceeds configured limit"
            )

        try:
            self._buf += b"\x00" * grow_by
        except BufferError as e:
            raise BufferError(
                "GrowBuffer: cannot grow while memoryviews are outstanding — "
                "release or materialize (bytes(mv)) previously yielded views "
                "before the next iter_lines_*/fill"
            ) from e
