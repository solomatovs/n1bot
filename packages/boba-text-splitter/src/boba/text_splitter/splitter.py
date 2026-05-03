"""TextSplitter: Converter[str, Iterable[str]] — lazy split с soft-break.

Реализация — generator: yield'ит чанки по мере готовности, не собирает в
`list`. Память — O(chunk_size), не O(N). Параметры зашиты в ctor —
экземпляр иммутабелен и переиспользуем (stateless converter).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.patterns import Converter, ConverterInputError

__all__ = ["TextSplitter"]


_SOFT_BREAKS = ("\n\n", "\n", ". ", " ")


class TextSplitter(Converter[str, Iterable[str]]):
    """str → Iterable[str]: chunk_size-based split с soft-break (lazy)."""

    def __init__(self, *, chunk_size: int, chunk_overlap: int) -> None:
        if chunk_size <= 0:
            msg = f"chunk_size must be > 0, got {chunk_size}"
            raise ConverterInputError(msg)
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            msg = (
                f"chunk_overlap must be in [0, chunk_size), got "
                f"{chunk_overlap} for chunk_size={chunk_size}"
            )
            raise ConverterInputError(msg)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    def convert(self, value: str) -> Iterator[str]:
        text = value.strip()
        if not text:
            return
        if len(text) <= self._chunk_size:
            yield text
            return
        start = 0
        while start < len(text):
            end = min(start + self._chunk_size, len(text))
            if end < len(text):
                end = _soft_break(text, start, end)
            piece = text[start:end].strip()
            if piece:
                yield piece
            if end == len(text):
                break
            start = max(end - self._chunk_overlap, start + 1)


def _soft_break(text: str, start: int, end: int) -> int:
    """Сдвинуть end к ближайшему разделителю слева; иначе hard break."""
    window = text[start:end]
    for sep in _SOFT_BREAKS:
        idx = window.rfind(sep)
        if idx != -1 and idx >= len(window) // 2:
            return start + idx + len(sep)
    return end
