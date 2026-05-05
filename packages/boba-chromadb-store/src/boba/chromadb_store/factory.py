"""ChromadbPersistStoreFactory: AppConfig → ChromadbPersistStore."""

from __future__ import annotations

from boba.chromadb_store.store import ChromadbPersistStore
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.indexing import (
    IndexerExtensionContext,
    Store,
    StoreFactory,
    StoreId,
)

__all__ = ["ChromadbPersistStoreFactory"]


class ChromadbPersistStoreFactory(StoreFactory):
    """Читает [ext.chromadb] (shared) и собирает ChromadbPersistStore."""

    def id(self) -> StoreId:
        return StoreId("ext.chromadb_persist")

    def produce(self, ctx: IndexerExtensionContext) -> Store:
        cfg = ctx.config.section(ChromadbSharedSection)
        if not cfg.persist_path:
            msg = "[ext.chromadb] persist_path is required for ChromadbPersistStore"
            raise ValueError(msg)
        return ChromadbPersistStore(cfg.persist_path)
