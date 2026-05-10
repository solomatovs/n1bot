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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import ChunkLocation

__all__ = [
    "LengthFunction",
    "RecursiveCharSplitter",
    "SplitPiece",
    "Splitter",
]

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
T_contra = TypeVar("T_contra", contravariant=True)


@dataclass(frozen=True, slots=True)
class Piece:
    """Внутренний кусок splitter: text + абсолютный start-offset в источнике"""

    text: str
    start: int


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

    def __call__(self, value: T_contra, /) -> int: ...


class RecursiveCharSplitter(Splitter[str]):
    """
    Recursive separator-based текстовый splitter с offset-tracking.

    Идём по separators от крупных к мелким, режем; куски ≥ chunk_size
    рекурсивно режем дальше; маленькие склеиваем тем же separator-glue в
    чанки ≤ chunk_size с фронт-усечением до chunk_overlap после flush.

    `SplitPiece.location.start`/`end` — char offsets в исходной строке.
    По построению `original[start:end]` совпадает с `content` для chunk'а,
    собранного из соседних кусков на одном уровне рекурсии.
    """

    DEFAULT_SEPARATORS: ClassVar[tuple[str, ...]] = ("\n\n", "\n", " ", "")

    def __init__(
        self,
        chunk_size: int,
        chunk_overlap: int = 0,
        separators: Sequence[str] | None = None,
        length_function: LengthFunction[str] | None = None,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._separators: tuple[str, ...] = (
            tuple(separators) if separators is not None else self.DEFAULT_SEPARATORS
        )
        self._length: LengthFunction[str] = (
            length_function if length_function is not None else len
        )

    def split(self, value: str) -> Iterable[SplitPiece[str]]:
        if not value:
            return

        for piece in self._split_recursive(value, 0, self._separators):
            yield SplitPiece(
                content=piece.text,
                location=ChunkLocation(
                    start=piece.start,
                    end=piece.start + len(piece.text),
                ),
            )

    def _split_recursive(
        self,
        text: str,
        base: int,
        seps: Sequence[str],
    ) -> list[Piece]:
        sep, next_seps = self._pick_separator(seps, text)
        splits = self._split_by_separator(text, base, sep)
        final: list[Piece] = []
        good: list[Piece] = []

        for p in splits:
            if self._length(p.text) < self._chunk_size:
                good.append(p)
                continue

            if good:
                final.extend(self._merge_pieces(good, sep))
                good = []

            if next_seps:
                final.extend(self._split_recursive(p.text, p.start, next_seps))
            else:
                final.append(p)

        if good:
            final.extend(self._merge_pieces(good, sep))

        return final

    @staticmethod
    def _pick_separator(
        seps: Sequence[str],
        text: str,
    ) -> tuple[str, Sequence[str]]:
        """
        Выбирает разделитель по принципу первый попавшийся из списка
        """
        for i, s in enumerate(seps):
            if s == "":
                return "", ()
            if s in text:
                return s, seps[i + 1 :]
        return (seps[-1] if seps else "", ())

    @staticmethod
    def _split_by_separator(text: str, base: int, sep: str) -> list[Piece]:
        """
            Разбивает текст на куски по выбранному разделителю
            Возвращает список объектов
            Piece = {
                text  : кусок текста
                start : offset начала куска внутри text
            }
        """
        if sep == "":
            return [Piece(text=c, start=base + i) for i, c in enumerate(text)]

        pieces: list[Piece] = []
        pos = 0
        n = len(text)
        sep_len = len(sep)
        while pos < n:
            idx = text.find(sep, pos)
            if idx == -1:
                pieces.append(Piece(text=text[pos:], start=base + pos))
                break
            if idx > pos:
                pieces.append(Piece(text=text[pos:idx], start=base + pos))
            pos = idx + sep_len
        return pieces

    def _merge_pieces(self, pieces: list[Piece], glue: str) -> list[Piece]:
        glue_len = self._length(glue)
        out: list[Piece] = []
        current: list[Piece] = []
        current_len = 0

        for p in pieces:
            plen = self._length(p.text)
            extra = plen + (glue_len if current else 0)
            if current_len + extra > self._chunk_size and current:
                out.append(self._join_batch(current, glue))
                while current and (
                    current_len > self._chunk_overlap
                    or current_len + extra > self._chunk_size
                ):
                    head_len = self._length(current[0].text) + (
                        glue_len if len(current) > 1 else 0
                    )
                    current_len -= head_len
                    current = current[1:]
                    extra = plen + (glue_len if current else 0)
            current.append(p)
            current_len += extra

        if current:
            out.append(self._join_batch(current, glue))
        return out

    @staticmethod
    def _join_batch(batch: list[Piece], glue: str) -> Piece:
        text = glue.join(p.text for p in batch)
        return Piece(text=text, start=batch[0].start)
