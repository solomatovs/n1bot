"""ChunkLocation — positional offset в исходном content; отдельный модуль против circular import sections/chunks."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ChunkLocation"]


@dataclass(frozen=True)
class ChunkLocation:
    """Положение в исходном content: start включительно, end исключительно (полуинтервал)."""

    start: int
    end: int
