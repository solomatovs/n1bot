"""Растущий байтовый буфер для накопления COPY-блоков (push-источник)."""

from __future__ import annotations

from psycopg.abc import Buffer

__all__ = ["BufferCapacityError", "CopyBuffer", "RowLimitExceededError"]


class BufferCapacityError(Exception):
    """COPY-поток превысил max_capacity буфера."""


class RowLimitExceededError(Exception):
    """COPY-поток дал больше data-строк, чем limit_rows."""


class CopyBuffer:
    """
    Байтовый буфер под COPY TEXT: копит блоки и трекает границы строк.
    """

    _INITIAL_CAPACITY = 4096
    _NL = 0x0A  # разделитель строк COPY TEXT (однозначный: внутриячеечные -> \n-эскейп)

    def __init__(self, *, max_capacity: int, limit_rows: int | None = None) -> None:
        if max_capacity <= 0:
            raise ValueError(f"max_capacity must be positive, got {max_capacity}")
        if limit_rows is not None and limit_rows < 0:
            raise ValueError(f"limit_rows must be >= 0, got {limit_rows}")
        self._max_capacity = max_capacity
        self._limit_rows = limit_rows
        self._buf = bytearray(min(self._INITIAL_CAPACITY, max_capacity))
        self._cap = len(self._buf)  # текущая ёмкость; меняется только в _grow
        self._size = 0
        self._nl_count = 0  # число \n (строк) накопленных; data-строк = -1 (header)

    @property
    def size(self) -> int:
        """Сколько байт накоплено."""
        return self._size

    @property
    def row_count(self) -> int:
        """Число data-строк (всего строк минус header)."""
        return max(0, self._nl_count - 1)

    def write(self, block: Buffer) -> None:
        """Дописать блок; досчитать строки; проверить оба гарда."""
        end = self._size + len(block)
        if end > self._cap:
            self._grow(end)
        self._buf[self._size : end] = block
        self._nl_count += self._buf.count(self._NL, self._size, end)
        self._size = end

        if self._limit_rows is not None and self.row_count > self._limit_rows:
            raise RowLimitExceededError

    def decode(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        """Декодировать накопленные байты (без копии-среза — через memoryview)."""
        return str(memoryview(self._buf)[: self._size], encoding, errors)

    def _grow(self, needed: int) -> None:
        """Удвоить ёмкость in-place до >= needed (cap по max_capacity)."""
        if needed > self._max_capacity:
            raise BufferCapacityError(
                f"CopyBuffer: {needed} bytes exceeds max_capacity "
                f"{self._max_capacity}",
            )
        target = self._cap
        while target < needed:
            target *= 2
        target = min(target, self._max_capacity)
        self._buf += b"\x00" * (target - self._cap)
        self._cap = target
