"""ChunkLocation — positional offset в исходном content.

Используется и `Section` (offset секции в raw документе), и `Chunk` (offset
чанка в raw документе). Вынесен в отдельный модуль чтобы избежать circular
import между `sections.py` и `chunks.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ChunkLocation"]


@dataclass(frozen=True)
class ChunkLocation:
    """Положение в исходном content.

    `start`/`end` — в естественных единицах T:
        char offsets для str,
        byte offsets для bytes,
        индексы для list-like.

    `start` включительно, `end` исключительно (полуинтервал).
    """

    start: int
    end: int
