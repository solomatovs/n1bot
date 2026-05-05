"""IndexPipeline: RequestSource → Transport → Reader → Chunker → Store.

Полностью streaming: каждое звено — generator, ни секции, ни чанки не
накапливаются в list'ы. Память — O(один документ + buffer Store'а).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Generic, TypeVar

from boba.indexing.chunker import Chunker
from boba.indexing.context import IndexingContext
from boba.indexing.raw_document import RawDocument
from boba.indexing.reader import Reader
from boba.indexing.request import Request
from boba.indexing.request_source import RequestSource
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.store import Store
from boba.indexing.transport import Transport
from boba.patterns import StateFull

__all__ = ["IndexPipeline"]

logger = logging.getLogger(__name__)

ReqT = TypeVar("ReqT", bound=Request)

_HASH_KEYS = ("etag", "version", "mtime", "last_modified")


class IndexPipeline(StateFull, Generic[ReqT]):
    """Оркестратор одного прогона индексации, generic по типу Request.

    Per-document инвариант:
    - Skip-if-unchanged: Pipeline ищет в `RawDocument.metadata` ключи
      `etag` / `version` / `mtime` / `last_modified` (в порядке приоритета),
      сравнивает с тем что в Store через `Store.content_hash_for(source_id)`.
      Если совпадает — документ пропускается.
    - Иначе: `Store.delete_by_source(source_id)` → reader → chunker →
      `Store.handle(chunk)` для каждого чанка.

    Per-document error isolation: try/except per item с инкрементом
    `sources_failed` в stats — одна сломанная страница не валит прогон.
    """

    def __init__(
        self,
        *,
        request_source: RequestSource[ReqT],
        transport: Transport[ReqT],
        reader: Reader,
        chunker: Chunker,
        store: Store,
    ) -> None:
        self._request_source = request_source
        self._transport = transport
        self._reader = reader
        self._chunker = chunker
        self._store = store

    def name(self) -> str:
        return (
            f"IndexPipeline({self._request_source.name()} → "
            f"{self._transport.name()} → {self._reader.name()} → "
            f"{self._chunker.name()} → {self._store.name()})"
        )

    @property
    def request_source(self) -> RequestSource[ReqT]:
        """Доступ к RequestSource — для sync-операций (list_source_ids)."""
        return self._request_source

    @property
    def store(self) -> Store:
        """Доступ к Store — для list/show/sync-операций CLI."""
        return self._store

    def reset(self) -> None:
        self._request_source.reset()
        self._transport.reset()
        self._reader.reset()
        self._chunker.reset()
        self._store.reset()

    def run(
        self, ctx: IndexingContext, *, description: str | None = None
    ) -> IndexStats:
        self._store.ensure_target(ctx, description)
        stats = IndexStatsBuilder()
        requests = self._request_source.stream(ctx)
        for raw_doc in self._transport.stream(ctx, requests):
            try:
                self._process_one(ctx, raw_doc, stats)
            except Exception as e:
                logger.warning(
                    "indexing failed for source_id=%r: %s: %s",
                    raw_doc.source_id,
                    type(e).__name__,
                    e,
                )
                stats.source_failed()
        self._store.flush(ctx)
        return stats.build()

    def _process_one(
        self,
        ctx: IndexingContext,
        raw_doc: RawDocument,
        stats: IndexStatsBuilder,
    ) -> None:
        new_hash = _content_hash_of(raw_doc)
        if new_hash:
            existing = self._store.content_hash_for(ctx, raw_doc.source_id)
            if existing and existing == new_hash:
                stats.source_skipped_unchanged()
                return

        stats.source_seen(raw_doc.source_id)
        stats.chunks_deleted_add(
            self._store.delete_by_source(ctx, raw_doc.source_id)
        )

        sections = self._reader.convert(ctx, raw_doc)
        # tap для подсчёта sections без list-накопления
        from collections.abc import Iterator  # noqa: PLC0415

        from boba.indexing.sections import Section  # noqa: PLC0415

        def _tap() -> Iterator[Section]:
            for s in sections:
                stats.section_emitted()
                yield s

        for chunk in self._chunker.stream(ctx, _tap()):
            # Pipeline кладёт hash из raw_doc.metadata в Chunk.content_hash —
            # Reader/Chunker про hash не знают, это политика Pipeline'а.
            stamped = replace(chunk, content_hash=new_hash) if new_hash else chunk
            self._store.handle(ctx, stamped)
            stats.chunk_upserted()


def _content_hash_of(raw_doc: RawDocument) -> str:
    """Извлечь skip-hash из `RawDocument.metadata` по приоритету ключей."""
    md = raw_doc.metadata
    for key in _HASH_KEYS:
        v = md.get(key)
        if v:
            return str(v)
    return ""
