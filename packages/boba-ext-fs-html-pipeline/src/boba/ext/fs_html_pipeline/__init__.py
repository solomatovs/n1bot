"""boba-ext-fs-html-pipeline: pipeline-плагин для индексации .html из FS."""

from __future__ import annotations

from boba.chromadb_store import ChromadbPersistStore
from boba.config.app import AppConfig
from boba.ext.chromadb_shared import ChromadbSharedSection, make_embedding_function
from boba.ext.fs_html_pipeline.config import (
    FsHtmlPipelineConfig,
    FsHtmlPipelineConfigSection,
)
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.html_reader import HtmlReader
from boba.indexing import IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "FsHtmlPipelineConfig",
    "FsHtmlPipelineConfigSection",
]


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(FsHtmlPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
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
        store=ChromadbPersistStore(
            persist_path=shared.persist_path,
            embedding_function=make_embedding_function(shared),
        ),
    )


PIPELINE = PipelineSpec(
    section=FsHtmlPipelineConfigSection(),
    build=_build,
)
