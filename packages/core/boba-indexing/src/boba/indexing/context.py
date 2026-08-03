"""Идентификатор коллекции в векторной базе."""

from __future__ import annotations

from typing import NewType

__all__ = ["CollectionId"]


CollectionId = NewType("CollectionId", str)
"""Идентификатор коллекции в векторной базе; на один backend — много коллекций."""
