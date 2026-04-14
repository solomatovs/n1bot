"""Растущий байтовый буфер для чтения строк из файла без повторных аллокаций.

Получает fd в конструкторе, не владеет им (не закрывает)::

    with open(path, "rb") as f:
        buf = GrowBuffer(f)
        for line in buf.iter_lines(b"\\n", offset=0):
            process(line)
        next_offset = buf.consumed
"""

from __future__ import annotations

from io import BufferedReader
from typing import Iterator


class GrowBuffer:
    """Байтовый буфер, который растёт но никогда не сжимается.

    Принимает fd в конструкторе, читает через readinto() прямо в bytearray.
    Не владеет fd — вызывающий код отвечает за открытие и закрытие.
    """

    _INITIAL_CAPACITY = 4096

    def __init__(self, fd: BufferedReader) -> None:
        self._fd = fd
        self._buf = bytearray(self._INITIAL_CAPACITY)
        self._size = 0
        self._consumed = 0

    @property
    def capacity(self) -> int:
        """Текущая ёмкость буфера (никогда не уменьшается)."""
        return len(self._buf)

    @property
    def consumed(self) -> int:
        """Количество байт, обработанных последним iter_lines()."""
        return self._consumed

    def iter_lines(self, separator: bytes, offset: int = 0) -> Iterator[memoryview]:
        """Seek + read + yield полные строки как memoryview.

        Читает от offset до конца файла прямо в буфер.
        Yield'ит строки между separator'ами.
        Неполная последняя строка не включается.
        Количество обработанных байт от offset доступно через ``consumed``.
        """
        self._fd.seek(offset)
        self._fill()

        if self._size == 0:
            self._consumed = 0
            return

        data = bytes(memoryview(self._buf)[: self._size])
        last_sep = data.rfind(separator)
        if last_sep == -1:
            self._consumed = 0
            return

        end = last_sep + len(separator)
        self._consumed = end

        sep_len = len(separator)
        start = 0
        while start < end:
            pos = data.find(separator, start, end)
            if pos == -1:
                break
            yield memoryview(self._buf)[start:pos]
            start = pos + sep_len

    def _fill(self) -> None:
        """Прочитать данные из fd в буфер (от текущей позиции fd)."""
        self._size = 0
        while True:
            if self._size == len(self._buf):
                self._grow()
            n = self._fd.readinto(memoryview(self._buf)[self._size :])
            if not n:
                break
            self._size += n

    def _grow(self) -> None:
        """Удвоить ёмкость буфера."""
        new_buf = bytearray(len(self._buf) * 2)
        new_buf[: self._size] = self._buf[: self._size]
        self._buf = new_buf
