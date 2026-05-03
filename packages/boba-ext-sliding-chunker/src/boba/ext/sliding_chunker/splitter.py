"""Character-based text splitter с soft-break'ами."""

from __future__ import annotations

__all__ = ["split_text"]


_SOFT_BREAKS = ("\n\n", "\n", ". ", " ")


def split_text(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    if chunk_size <= 0:
        msg = f"chunk_size must be > 0, got {chunk_size}"
        raise ValueError(msg)
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        msg = (
            f"chunk_overlap must be in [0, chunk_size), got "
            f"{chunk_overlap} for chunk_size={chunk_size}"
        )
        raise ValueError(msg)

    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            end = _soft_break(text, start, end)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(text):
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks


def _soft_break(text: str, start: int, end: int) -> int:
    """Сдвинуть end к ближайшему разделителю слева; иначе hard break."""
    window = text[start:end]
    for sep in _SOFT_BREAKS:
        idx = window.rfind(sep)
        if idx != -1 and idx >= len(window) // 2:
            return start + idx + len(sep)
    return end
