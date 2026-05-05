"""boba-ext-fs-text-pipeline: pipeline-плагин для индексации текстовых файлов из FS."""

from __future__ import annotations

from boba.chromadb_store import ChromadbPersistStore
from boba.config.app import AppConfig
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.fs_text_pipeline.config import (
    FsTextPipelineConfig,
    FsTextPipelineConfigSection,
)
from boba.fs_transport import FsTransport, FsWalkRequestSource
from boba.indexing import IndexPipeline, PipelineSpec
from boba.sliding_chunker import SlidingChunker, SlidingChunkerConfig
from boba.text_reader import TextReader

__all__ = [
    "PIPELINE",
    "FsTextPipelineConfig",
    "FsTextPipelineConfigSection",
]


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(FsTextPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
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


PIPELINE = PipelineSpec(
    section=FsTextPipelineConfigSection(),
    build=_build,
)
