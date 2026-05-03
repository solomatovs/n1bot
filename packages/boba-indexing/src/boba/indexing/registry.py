"""Реестры Reader'ов и Source'ов на (Context)CatalogFactory."""

from __future__ import annotations

from boba.indexing.chunker import Chunker, ChunkerFactory, ChunkerId
from boba.indexing.extension import IndexerExtensionContext
from boba.indexing.reader import Reader, ReaderDispatcher, ReaderId
from boba.indexing.source import Source, SourceFactory, SourceId
from boba.indexing.store import Store, StoreFactory, StoreId
from boba.patterns import CatalogFactory, ContextCatalogFactory, ItemProvider

__all__ = [
    "ChunkerRegistry",
    "ReaderProvider",
    "ReaderRegistry",
    "SourceRegistry",
    "StoreRegistry",
]


class ReaderProvider(ItemProvider[ReaderId, Reader]):
    """Тонкая обёртка Reader → ItemProvider для CatalogFactory."""

    def __init__(self, reader: Reader) -> None:
        self._reader = reader

    def id(self) -> ReaderId:
        return self._reader.reader_id()

    def produce(self) -> Reader:
        return self._reader


class ReaderRegistry(
    CatalogFactory[ReaderId, Reader, ReaderDispatcher],
):
    """Собирает зарегистрированные Reader'ы в готовый ReaderDispatcher.

    Повторная регистрация по тому же ReaderId — silent overwrite (last-wins).
    Порядок dispatcher'а — порядок регистрации (CPython dict insertion order).
    """

    def __init__(self, *, skip_unmatched: bool = False) -> None:
        super().__init__()
        self._skip_unmatched = skip_unmatched

    def register_reader(self, reader: Reader) -> None:
        self.register(ReaderProvider(reader))

    def finalize(self, items: dict[ReaderId, Reader]) -> ReaderDispatcher:
        return ReaderDispatcher(
            list(items.values()),
            skip_unmatched=self._skip_unmatched,
        )


class ChunkerRegistry(
    ContextCatalogFactory[
        IndexerExtensionContext,
        ChunkerId,
        Chunker,
        dict[ChunkerId, Chunker],
    ],
):
    """Каталог ChunkerFactory; build(ctx) → dict[ChunkerId, готовый Chunker]."""

    def register_factory(self, factory: ChunkerFactory) -> None:
        self.register(factory)

    def finalize(
        self,
        ctx: IndexerExtensionContext,
        items: dict[ChunkerId, Chunker],
    ) -> dict[ChunkerId, Chunker]:
        del ctx
        return dict(items)


class SourceRegistry(
    ContextCatalogFactory[
        IndexerExtensionContext,
        SourceId,
        Source,
        dict[SourceId, Source],
    ],
):
    """Каталог SourceFactory; build(ctx) → dict[SourceId, готовый Source].

    Каждая SourceFactory читает свою ConfigSection через ctx и собирает
    Source с применёнными параметрами. Lookup по SourceId — для конфига
    pipeline (`source = "ext.fs"`).
    """

    def register_factory(self, factory: SourceFactory) -> None:
        self.register(factory)

    def finalize(
        self,
        ctx: IndexerExtensionContext,
        items: dict[SourceId, Source],
    ) -> dict[SourceId, Source]:
        del ctx
        return dict(items)


class StoreRegistry(
    ContextCatalogFactory[
        IndexerExtensionContext,
        StoreId,
        Store,
        dict[StoreId, Store],
    ],
):
    """Каталог StoreFactory; build(ctx) → dict[StoreId, готовый Store]."""

    def register_factory(self, factory: StoreFactory) -> None:
        self.register(factory)

    def finalize(
        self,
        ctx: IndexerExtensionContext,
        items: dict[StoreId, Store],
    ) -> dict[StoreId, Store]:
        del ctx
        return dict(items)
