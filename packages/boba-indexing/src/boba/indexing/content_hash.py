"""
ContentHash — типизированное hash-значение чанка

Базовый ABC + три встроенные реализации:
1. `BytesContentHash`  — сырые байты digest (sha256/blake2b raw output).
  В 2 компактнее чем hex, нативно сравнивается

2. `IntContentHash`— 64-bit (или больше) int (xxhash, fnv)
  быстрое сравнение через ==

3. `StringContentHash` — строка
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = [
    "BytesContentHash",
    "ContentHash",
    "IntContentHash",
    "StringContentHash",
]


class ContentHash(ABC):
    """
    Hash-значение чанка
    """

    @abstractmethod
    def to_wire(self) -> str:
        """Wire-format для записи в RecordManager (обычно hex-string)"""
        ...


@dataclass(frozen=True)
class BytesContentHash(ContentHash):
    """Hash как сырые байты digest'а (sha256/blake2b/...)"""

    raw: bytes

    def to_wire(self) -> str:
        return self.raw.hex()


@dataclass(frozen=True)
class IntContentHash(ContentHash):
    """
    Hash как N-битное целое, сериализуется в hex
    """

    value: int
    bits: int = 64

    def to_wire(self) -> str:
        hex_chars = self.bits // 4
        return f"{self.value:0{hex_chars}x}"


@dataclass(frozen=True)
class StringContentHash(ContentHash):
    """
    Hash уже в строковом виде
    hex / base32 / base64 — на усмотрение реализации
    """

    text: str

    def to_wire(self) -> str:
        return self.text
