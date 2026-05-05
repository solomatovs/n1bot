"""ConfluenceCqlPipelineFactory."""

from __future__ import annotations

from typing import Any

from boba.chromadb_store import ChromadbPersistStore
from boba.confluence_reader import ConfluenceReader
from boba.confluence_requests import ConfluenceCqlRequestSource
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.confluence_cql_pipeline.config import (
    ConfluenceCqlPipelineConfig,
    ConfluenceCqlPipelineConfigSection,
)
from boba.heading_chunker import HeadingChunker, HeadingChunkerConfig
from boba.http_transport import BasicAuth, HttpTransport, PatAuth
from boba.indexing import (
    AuthApplier,
    IndexerExtensionContext,
    IndexPipeline,
    PipelineFactory,
    PipelineId,
)

__all__ = ["ConfluenceCqlPipelineFactory"]


class ConfluenceCqlPipelineFactory(PipelineFactory):
    """`ext.confluence_cql` pipeline."""

    def id(self) -> PipelineId:
        return PipelineId("ext.confluence_cql")

    def produce(self, ctx: IndexerExtensionContext) -> IndexPipeline[Any]:
        cfg = ctx.config.section(ConfluenceCqlPipelineConfigSection)
        shared = ctx.config.section(ChromadbSharedSection)
        _validate(cfg)

        auth = _build_auth(cfg)
        request_source = ConfluenceCqlRequestSource(
            base_url=cfg.base_url,
            auth=auth,
            cql=cfg.cql,
            body_format=cfg.body_format,
            timeout_sec=cfg.timeout_sec,
        )
        return IndexPipeline(
            request_source=request_source,
            transport=HttpTransport(timeout_sec=cfg.timeout_sec),
            reader=ConfluenceReader(),
            chunker=HeadingChunker(
                HeadingChunkerConfig(
                    chunk_size=cfg.chunk_size,
                    chunk_overlap=cfg.chunk_overlap,
                )
            ),
            store=ChromadbPersistStore(persist_path=shared.persist_path),
        )


def _validate(cfg: ConfluenceCqlPipelineConfig) -> None:
    if not cfg.base_url:
        msg = (
            "base_url пуст; env "
            "BOBA_INDEXER__PIPELINES__CONFLUENCE_CQL__BASE_URL."
        )
        raise ValueError(msg)
    if not cfg.cql:
        msg = "cql пуст; обязательное поле для confluence_cql."
        raise ValueError(msg)
    if not cfg.auth_token:
        msg = (
            "auth_token пуст; env "
            "BOBA_INDEXER__PIPELINES__CONFLUENCE_CQL__AUTH_TOKEN."
        )
        raise ValueError(msg)


def _build_auth(cfg: ConfluenceCqlPipelineConfig) -> AuthApplier:
    if cfg.auth_method == "basic":
        if not cfg.auth_user:
            msg = "auth_method=basic требует auth_user."
            raise ValueError(msg)
        return BasicAuth(user=cfg.auth_user, password=cfg.auth_token)
    return PatAuth(token=cfg.auth_token)
