"""boba-ext-confluence-space-pipeline: индексация space'а Confluence."""

from __future__ import annotations

from boba.chromadb_store import ChromadbPersistStore
from boba.config.app import AppConfig
from boba.confluence_reader import ConfluenceJsonDecoder, ConfluenceReader
from boba.confluence_requests import ConfluenceSpaceRequestSource
from boba.confluence_shared import ConfluenceConnection
from boba.ext.chromadb_shared import ChromadbSharedSection, make_embedding_function
from boba.ext.confluence_space_pipeline.config import (
    ConfluenceSpacePipelineConfig,
    ConfluenceSpacePipelineConfigSection,
)
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.indexing import IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "ConfluenceSpacePipelineConfig",
    "ConfluenceSpacePipelineConfigSection",
]


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(ConfluenceSpacePipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=ConfluenceSpaceRequestSource(
            base_url=cfg.base_url,
            auth=ConfluenceConnection.make_auth(cfg),
            space_key=cfg.space_key,
            body_format=cfg.body_format,
            timeout_sec=cfg.timeout_sec,
        ),
        transport=ConfluenceConnection.make_transport(cfg),
        decoder=ConfluenceJsonDecoder(body_format=cfg.body_format),
        reader=ConfluenceReader(),
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
    section=ConfluenceSpacePipelineConfigSection(),
    build=_build,
)
