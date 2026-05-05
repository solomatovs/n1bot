"""boba-ext-fs-markdown-pipeline: pipeline-плагин для индексации .md из FS."""

from __future__ import annotations

from boba.chromadb_store import ChromadbPersistStore
from boba.config.app import AppConfig
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.fs_markdown_pipeline.config import (
    FsMarkdownPipelineConfig,
    FsMarkdownPipelineConfigSection,
)
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.indexing import IndexPipeline, PipelineSpec
from boba.markdown_reader import MarkdownReader

__all__ = [
    "PIPELINE",
    "FsMarkdownPipelineConfig",
    "FsMarkdownPipelineConfigSection",
]


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(FsMarkdownPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=FsWalkRequestSource(
            paths=cfg.paths,
            include=cfg.include,
            exclude=cfg.exclude,
            follow_symlinks=cfg.follow_symlinks,
        ),
        transport=FsTransport(),
        reader=MarkdownReader(),
        chunker=HeadingChunker(
            HeadingChunkerConfig(
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
            )
        ),
        store=ChromadbPersistStore(persist_path=shared.persist_path),
    )


PIPELINE = PipelineSpec(
    section=FsMarkdownPipelineConfigSection(),
    build=_build,
)
