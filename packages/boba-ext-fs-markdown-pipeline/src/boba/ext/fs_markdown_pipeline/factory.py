"""FsMarkdownPipelineFactory: собирает IndexPipeline из конкретных компонентов."""

from __future__ import annotations

from typing import Any

from boba.chromadb_store import ChromadbPersistStore
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.fs_markdown_pipeline.config import FsMarkdownPipelineConfigSection
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.indexing import (
    IndexerExtensionContext,
    IndexPipeline,
    PipelineFactory,
    PipelineId,
)
from boba.markdown_reader import MarkdownReader

__all__ = ["FsMarkdownPipelineFactory"]


class FsMarkdownPipelineFactory(PipelineFactory):
    """`ext.fs_markdown` pipeline."""

    def id(self) -> PipelineId:
        return PipelineId("ext.fs_markdown")

    def produce(self, ctx: IndexerExtensionContext) -> IndexPipeline[Any]:
        cfg = ctx.config.section(FsMarkdownPipelineConfigSection)
        shared = ctx.config.section(ChromadbSharedSection)
        if not cfg.paths:
            msg = (
                "fs_markdown pipeline: пустой `paths`; задайте через TOML "
                "`[indexer.pipelines.fs_markdown] paths = [...]` или env."
            )
            raise ValueError(msg)

        request_source = FsWalkRequestSource(
            paths=cfg.paths,
            include=cfg.include,
            exclude=cfg.exclude,
            follow_symlinks=cfg.follow_symlinks,
        )
        transport = FsTransport()
        reader = MarkdownReader()
        chunker = HeadingChunker(
            HeadingChunkerConfig(
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
            )
        )
        store = ChromadbPersistStore(persist_path=shared.persist_path)

        return IndexPipeline(
            request_source=request_source,
            transport=transport,
            reader=reader,
            chunker=chunker,
            store=store,
        )
