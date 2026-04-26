"""Простой character-based text splitter.

Логика:

1. Если текст короче chunk_size — один чанк, без splitting.
2. Иначе режем по фиксированному окну chunk_size с overlap chunk_overlap.
3. Внутри окна стараемся резать по последнему \\n (или пробелу), чтобы
   не рвать слова/строки посередине. Если разделителя нет — режем по
   жёсткому offset.

Без сторонних библиотек — ни langchain, ни tiktoken: для
.md/.txt character-window работает достаточно хорошо, а для
HTML/PDF мы потом добавим формат-специфичный pre-processing в
reader'ах.
"""

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
    """Сдвигает end к ближайшему разделителю слева. Если ничего
    приемлемого в окне [start, end) нет — возвращает end как
    есть (hard break).
    """
    window = text[start:end]
    for sep in _SOFT_BREAKS:
        idx = window.rfind(sep)
        # требуем чтобы break был хотя бы в середине окна, иначе
        # чанки получатся слишком мелкими
        if idx != -1 and idx >= len(window) // 2:
            return start + idx + len(sep)
    return end
