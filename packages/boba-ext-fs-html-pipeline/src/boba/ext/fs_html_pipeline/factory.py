"""FsHtmlPipelineFactory."""

from __future__ import annotations

from typing import Any

from boba.chromadb_store import ChromadbPersistStore
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.fs_html_pipeline.config import FsHtmlPipelineConfigSection
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.html_reader import HtmlReader
from boba.indexing import (
    IndexerExtensionContext,
    IndexPipeline,
    PipelineFactory,
    PipelineId,
)

__all__ = ["FsHtmlPipelineFactory"]


class FsHtmlPipelineFactory(PipelineFactory):
    """`ext.fs_html` pipeline."""

    def id(self) -> PipelineId:
        return PipelineId("ext.fs_html")

    def produce(self, ctx: IndexerExtensionContext) -> IndexPipeline[Any]:
        cfg = ctx.config.section(FsHtmlPipelineConfigSection)
        shared = ctx.config.section(ChromadbSharedSection)
        if not cfg.paths:
            msg = (
                "fs_html pipeline: пустой `paths`; задайте через "
                "`[indexer.pipelines.fs_html] paths = [...]` или env."
            )
            raise ValueError(msg)
        return IndexPipeline(
            request_source=FsWalkRequestSource(
                paths=cfg.paths,
                include=cfg.include,
                exclude=cfg.exclude,
                follow_symlinks=cfg.follow_symlinks,
            ),
            transport=FsTransport(),
            reader=HtmlReader(),
            chunker=HeadingChunker(
                HeadingChunkerConfig(
                    chunk_size=cfg.chunk_size,
                    chunk_overlap=cfg.chunk_overlap,
                )
            ),
            store=ChromadbPersistStore(persist_path=shared.persist_path),
        )
