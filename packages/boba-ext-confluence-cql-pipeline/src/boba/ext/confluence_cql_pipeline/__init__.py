"""boba-ext-confluence-cql-pipeline: pipeline-плагин для индексации по CQL-запросу."""

from __future__ import annotations

from boba.chromadb_store import ChromadbPersistStore
from boba.config.app import AppConfig
from boba.confluence_reader import ConfluenceJsonDecoder, ConfluenceReader
from boba.confluence_requests import ConfluenceCqlRequestSource
from boba.ext.chromadb_shared import ChromadbSharedSection, make_embedding_function
from boba.ext.confluence_cql_pipeline.config import (
    ConfluenceCqlPipelineConfig,
    ConfluenceCqlPipelineConfigSection,
)
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.http_transport import BasicAuth, HttpTransport, PatAuth
from boba.indexing import AuthApplier, IndexPipeline, PipelineSpec

__all__ = [
    "PIPELINE",
    "ConfluenceCqlPipelineConfig",
    "ConfluenceCqlPipelineConfigSection",
]


def _build_auth(cfg: ConfluenceCqlPipelineConfig) -> AuthApplier:
    match cfg.auth_method:
        case "basic":
            return BasicAuth(user=cfg.auth_user, password=cfg.auth_token)
        case "pat":
            return PatAuth(token=cfg.auth_token)
        case _:
            msg = f"Unsupported auth_method: {cfg.auth_method}"
            raise ValueError(msg)


def _build(app: AppConfig) -> IndexPipeline:
    cfg = app.section(ConfluenceCqlPipelineConfigSection)
    shared = app.section(ChromadbSharedSection)
    return IndexPipeline(
        request_source=ConfluenceCqlRequestSource(
            base_url=cfg.base_url,
            auth=_build_auth(cfg),
            cql=cfg.cql,
            body_format=cfg.body_format,
            timeout_sec=cfg.timeout_sec,
        ),
        transport=HttpTransport(timeout_sec=cfg.timeout_sec),
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
    section=ConfluenceCqlPipelineConfigSection(),
    build=_build,
)
