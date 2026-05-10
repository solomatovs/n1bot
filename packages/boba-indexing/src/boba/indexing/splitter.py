from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import ChunkLocation

__all__ = [
    "LengthFunction",
    "OverlapCharSplitter",
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
    Один кусок, выданный Splitter:
    content + location в исходнике
    """

    content: T
    location: ChunkLocation


@runtime_checkable
class Splitter(Protocol[T]):
    def split(self, value: T) -> Iterable[SplitPiece[T]]: ...


@runtime_checkable
class LengthFunction(Protocol[T_contra]):
    def __call__(self, value: T_contra, /) -> int: ...


class OverlapCharSplitter(Splitter[str]):
    """
    Разделяет `value` на чанки внахлест:

        Схема:
        ```python
        # исходный текст
        index:      0  1  2  3  4  5  6  7  8  9 10 11 12 13
        value:      a  b     c  d     e  f     g  h     i  j

        # полученные чанки
        chunk 1:    a  b     c  d     e  f                       value[0:8]  = "ab cd ef"
        chunk 2:             c  d     e  f     g  h              value[3:11] = "cd ef gh"
        chunk 3:                      e  f     g  h     i  j     value[6:14] = "ef gh ij"
        ```

        Пример:
        ```python
        value = "ab cd ef gh ij"
        #        0  3  6  9  12

        s = RecursiveCharSplitter(
            chunk_size=8,         # размер одного чанка
            chunk_overlap=5,      # перекрытие соседних чанков (≈ символов)
            separators=[" "],     # приоритет: крупные → мелкие; "" — посимвольно
            length_function=None, # функция длины; None ≡ len (char-count)
        )

        list(s.split(value)) == [
            SplitPiece("ab cd ef", ChunkLocation(start=0, end=8)),
            SplitPiece("cd ef gh", ChunkLocation(start=3, end=11)),
            SplitPiece("ef gh ij", ChunkLocation(start=6, end=14)),
        ]
        ```
    """  # noqa: E501

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

        for piece in self._split_recursive(value, value, 0, self._separators):
            yield SplitPiece(
                content=piece.text,
                location=ChunkLocation(
                    start=piece.start,
                    end=piece.start + len(piece.text),
                ),
            )

    def _split_recursive(
        self,
        original: str,
        text: str,
        base: int,
        seps: Sequence[str],
    ) -> Iterator[Piece]:
        sep, next_seps = self._pick_separator(seps, text)
        good: deque[Piece] = deque()

        for p in self._split_by_separator(text, base, sep):
            if self._length(p.text) < self._chunk_size:
                good.append(p)
                continue

            if good:
                yield from self._merge_pieces(original, good, sep)
                good.clear()

            if next_seps:
                yield from self._split_recursive(original, p.text, p.start, next_seps)
            else:
                yield p

        if good:
            yield from self._merge_pieces(original, good, sep)

    @staticmethod
    def _pick_separator(
        seps: Sequence[str],
        text: str,
    ) -> tuple[str, Sequence[str]]:
        """Выбирает первый разделитель из списка, встречающийся в тексте."""
        for i, s in enumerate(seps):
            if s == "":
                return "", ()
            if s in text:
                return s, seps[i + 1 :]
        return (seps[-1] if seps else "", ())

    @staticmethod
    def _split_by_separator(text: str, base: int, sep: str) -> Iterator[Piece]:
        """Стримит куски, разделённые sep, с абсолютными offset от base."""
        if sep == "":
            for i, c in enumerate(text):
                yield Piece(text=c, start=base + i)
            return

        pos = 0
        n = len(text)
        sep_len = len(sep)
        while pos < n:
            idx = text.find(sep, pos)
            if idx == -1:
                yield Piece(text=text[pos:], start=base + pos)
                return
            if idx > pos:
                yield Piece(text=text[pos:idx], start=base + pos)
            pos = idx + sep_len

    def _merge_pieces(
        self,
        original: str,
        pieces: Iterable[Piece],
        glue: str,
    ) -> Iterator[Piece]:
        glue_len = self._length(glue)
        current: deque[Piece] = deque()
        current_len = 0

        for p in pieces:
            plen = self._length(p.text)
            extra = plen + (glue_len if current else 0)
            if current_len + extra > self._chunk_size and current:
                yield self._join_batch(original, current)
                while current and (
                    current_len > self._chunk_overlap
                    or current_len + extra > self._chunk_size
                ):
                    head_len = self._length(current[0].text) + (
                        glue_len if len(current) > 1 else 0
                    )
                    current_len -= head_len
                    current.popleft()
                    extra = plen + (glue_len if current else 0)
            current.append(p)
            current_len += extra

        if current:
            yield self._join_batch(original, current)

    @staticmethod
    def _join_batch(original: str, batch: deque[Piece]) -> Piece:
        # На одном уровне рекурсии соседние pieces разделены ровно тем же sep,
        # поэтому склейка эквивалентна срезу original без аллокаций glue.join.
        first = batch[0]
        last = batch[-1]
        return Piece(
            text=original[first.start : last.start + len(last.text)],
            start=first.start,
        )
