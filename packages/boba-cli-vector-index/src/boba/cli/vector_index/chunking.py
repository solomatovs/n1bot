"""Простой character-based text splitter."""

from __future__ import annotations

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200

_SOFT_BREAKS = ("\n\n", "\n", ". ", " ")


def split_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap must be in [0, chunk_size), got "
            f"{chunk_overlap} for chunk_size={chunk_size}"
        )

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
        # break должен быть хотя бы в середине окна
        if idx != -1 and idx >= len(window) // 2:
            return start + idx + len(sep)
    return end
