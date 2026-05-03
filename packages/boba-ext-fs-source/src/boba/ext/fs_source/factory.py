"""FsSourceFactory: AppConfig → FsSource."""

from __future__ import annotations

from boba.ext.fs_source.config import FsSourceConfigSection
from boba.ext.fs_source.source import FsSource
from boba.indexing import (
    IndexerExtensionContext,
    Source,
    SourceFactory,
    SourceId,
)

__all__ = ["FsSourceFactory"]


class FsSourceFactory(SourceFactory):
    """Читает [indexer.sources.fs] из AppConfig и собирает FsSource."""

    def id(self) -> SourceId:
        return SourceId("ext.fs")

    def produce(self, ctx: IndexerExtensionContext) -> Source:
        cfg = ctx.config.section(FsSourceConfigSection)
        return FsSource(cfg)
