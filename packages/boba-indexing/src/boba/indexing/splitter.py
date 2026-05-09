"""
Splitter[T] и LengthFunction[T] — протоколы для нарезки content'а
на куски с трекингом offset'ов в исходнике.

- `Splitter[T]` — режет один content на куски
с трекингом offset'а в исходнике (ChunkLocation),
чтобы потребитель (SectionChunker) мог честно проставить
`Chunk[T].location` без угадывания.

- `LengthFunction[T]` — функция длины content'а в естественных единицах
  `char-count` для str
  `token-count` для tokenizer-aware splitter'а
  byte-count для bytes

Инжектится в splitter для подсчета размера чанков в нужных еденицах
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import ChunkLocation

__all__ = ["LengthFunction", "SplitPiece", "Splitter"]

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
T_contra = TypeVar("T_contra", contravariant=True)


@dataclass(frozen=True)
class SplitPiece(Generic[T]):
    """
    Один кусок, выданный Splitter'ом:
    content + location в исходнике
    """

    content: T
    location: ChunkLocation


@runtime_checkable
class Splitter(Protocol[T]):
    """
    T → Iterable[SplitPiece[T]]: нарезка content'а с offset-трекингом

    Контракт:
      `location.start`/`location.end` — offset в исходном content
      start/end могут быть как номенами строк, так и байтовыми смещениями
      в зависимости от типа T и логики Splitterа.
      Например, для str это могут быть char offsets,
      для bytes — byte offsets
    """

    def split(self, value: T) -> Iterable[SplitPiece[T]]: ...


@runtime_checkable
class LengthFunction(Protocol[T_contra]):
    """
    Функция длины content в естественных единицах.

    Используется splitter для проверки `chunk_size`.
    Реализация по умолчаниюдля текста — `len`,
    но можно подменить на token-counter (tiktoken, huggingface-tokenizer)
    тогда chunk_size становится «не больше N токенов».
    """

    def __call__(self, value: T_contra) -> int: ...
