"""Чтение дескрипторов запущенных процессов в переиспользуемый буфер.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import io
from typing import ClassVar

__all__ = ["FdReader"]


class FdReader:
    """Чтение дескриптора в переиспользуемый буфер: одна аллокация на поток.

    Единственный способ читать каналы процессов: `os.read` выделял бы новый
    объект на каждую порцию, а порции идут десятками тысяч. Дескриптор
    ожидается блокирующим и опрашивается через select — частичное чтение
    штатно, `readinto` возвращает 0 только на EOF.
    """

    CHUNK: ClassVar[int] = 65536
    """Ёмкость пайпа Linux: больший буфер лишь удорожает чтение."""

    EMPTY: ClassVar[memoryview] = memoryview(b"")

    def __init__(self, fd: int, chunk: int = CHUNK) -> None:
        if chunk <= 0:
            msg = f"pipe reader of fd {fd}: chunk must be positive, got {chunk}"
            raise ValueError(msg)

        self._raw = io.FileIO(fd, "r", closefd=False)
        self._view = memoryview(bytearray(chunk))

    @property
    def fd(self) -> int:
        return self._raw.fileno()

    def read(self) -> memoryview:
        """Очередная порция; пустой view — EOF, дескриптор дочитан."""
        got = self._raw.readinto(self._view)
        if not got:
            return self.EMPTY

        return self._view[:got]
