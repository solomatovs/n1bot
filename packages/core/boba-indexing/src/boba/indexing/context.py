"""
Идентификатор коллекции в векторной базе.
"""

from __future__ import annotations

from typing import NewType

__all__ = ["CollectionId"]


CollectionId = NewType("CollectionId", str)
"""Идентификатор коллекции в векторной базе (Chroma/Qdrant collection).

Бэкэнд-уровневый scope: всё, что лежит в одной collection, физически
хранится вместе. На один backend — много коллекций.
"""
