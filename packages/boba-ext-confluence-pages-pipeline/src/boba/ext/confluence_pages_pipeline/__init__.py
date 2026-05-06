"""boba-ext-confluence-pages-pipeline: индексация страниц Confluence."""

from __future__ import annotations

from boba.chromadb_store import ChromadbPersistStore
from boba.config.app import AppConfig
from boba.confluence_reader import ConfluenceJsonDecoder, ConfluenceReader
from boba.confluence_requests import ConfluencePagesRequestSource
from boba.confluence_shared import ConfluenceConnection
from boba.ext.chromadb_shared import ChromadbSharedSection, make_embedding_function
from boba.ext.confluence_pages_pipeline.config import (
    ConfluencePagesPipelineConfig,
    ConfluencePagesPipelineConfigSection,
)
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.indexing import IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "ConfluencePagesPipelineConfig",
    "ConfluencePagesPipelineConfigSection",
]


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(ConfluencePagesPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=ConfluencePagesRequestSource(
            base_url=cfg.base_url,
            auth=ConfluenceConnection.make_auth(cfg),
            page_ids=cfg.page_ids,
            body_format=cfg.body_format,
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
    section=ConfluencePagesPipelineConfigSection(),
    build=_build,
)
