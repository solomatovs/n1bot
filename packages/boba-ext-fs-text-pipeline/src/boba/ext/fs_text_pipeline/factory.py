"""FsTextPipelineFactory."""

from __future__ import annotations

from typing import Any

from boba.chromadb_store import ChromadbPersistStore
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.fs_text_pipeline.config import FsTextPipelineConfigSection
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.indexing import (
    IndexerExtensionContext,
    IndexPipeline,
    PipelineFactory,
    PipelineId,
)
from boba.sliding_chunker import SlidingChunker, SlidingChunkerConfig
from boba.text_reader import TextReader

__all__ = ["FsTextPipelineFactory"]


class FsTextPipelineFactory(PipelineFactory):
    """`ext.fs_text` pipeline."""

    def id(self) -> PipelineId:
        return PipelineId("ext.fs_text")

    def produce(self, ctx: IndexerExtensionContext) -> IndexPipeline[Any]:
        cfg = ctx.config.section(FsTextPipelineConfigSection)
        shared = ctx.config.section(ChromadbSharedSection)
        if not cfg.paths:
            msg = (
                "fs_text pipeline: пустой `paths`; задайте через "
                "`[indexer.pipelines.fs_text] paths = [...]` или env."
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
            reader=TextReader(),
            chunker=SlidingChunker(
                SlidingChunkerConfig(
                    chunk_size=cfg.chunk_size,
                    chunk_overlap=cfg.chunk_overlap,
                )
            ),
            store=ChromadbPersistStore(persist_path=shared.persist_path),
        )
