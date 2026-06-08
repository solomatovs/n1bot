"""Splitter[T] — контракт «разбить контент на куски с сохранением offset'ов».

В домене живёт только interface + DTO + LengthFunction-протокол.
Конкретные реализации (OverlapCharSplitter для текста и т.п.) — в
format/feature-package'ах (например boba-text).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import ChunkLocation

__all__ = [
    "LengthFunction",
    "SplitPiece",
    "Splitter",
]

T = TypeVar("T")
T_contra = TypeVar("T_contra", contravariant=True)


@dataclass(frozen=True)
class SplitPiece(Generic[T]):
    """Один кусок, выданный Splitter: content + location в исходнике."""

    content: T
    location: ChunkLocation


@runtime_checkable
class Splitter(Protocol[T]):
    """Разделяет контент на куски с сохранением оригинального смещения.

    **Ответственность**:
    - разделить T на отдельные SplitPiece[T] в потоке;
    - сохранить исходные смещения внутри T;
    - сохранить исходный контент из T в диапазоне location.

    **Схема**:
    python
    T   ──────splitter.split──->  Iterable[SplitPiece[T]]
                              ->    content  : T
                              ->    location : ChunkLocation
    

    **Пример минимальной реализации**:
    python
    class HalfSplitter(Splitter[str]):
        def split(self, value: str) -> Iterable[SplitPiece[str]]:
            mid = len(value) // 2
            yield SplitPiece(value[:mid], ChunkLocation(start=0, end=mid))
            yield SplitPiece(value[mid:], ChunkLocation(start=mid, end=len(value)))
    
    """

    def split(self, value: T) -> Iterable[SplitPiece[T]]: ...


@runtime_checkable
class LengthFunction(Protocol[T_contra]):
    """Функция длины content в естественных единицах.

    Инжектится в Splitter. Реализации:
    - len — char-count для str, byte-count для bytes (default).
    - tokenizer-aware (tiktoken / hf-tokenizer) — token-count;
      тогда chunk_size у splitter'а становится «не больше N токенов».
    """

    def __call__(self, value: T_contra, /) -> int: ...
