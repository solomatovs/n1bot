"""Boba indexing extension: ChromaDB persistent store (write-side)."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.chromadb_store.factory import ChromadbPersistStoreFactory
from boba.ext.chromadb_store.store import ChromadbPersistStore
from boba.indexing import IndexerExtensionContext, StoreFactory

__all__ = [
    "ChromadbPersistStore",
    "ChromadbPersistStoreFactory",
    "register_stores",
]


def register_stores(
    ctx: IndexerExtensionContext,
) -> Iterable[StoreFactory]:
    """Entry-point boba.indexing.stores."""
    del ctx
    yield ChromadbPersistStoreFactory()
