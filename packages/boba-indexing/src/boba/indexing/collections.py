"""CollectionInfo: нейтральное описание именованной группы векторов в Store."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CollectionInfo"]


@dataclass(frozen=True)
class CollectionInfo:
    """Логическая группа векторов в Store (collection в Chroma/Qdrant и т.п.)."""

    name: str
    description: str
    count: int
