"""PrintStats: что напечатал PrintPipeline за один прогон."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PrintStats"]


@dataclass(frozen=True)
class PrintStats:
    """Сводка прогона PrintPipeline: сколько чанков и уникальных source_id выведено."""

    chunks_printed: int
    sources_seen: int
