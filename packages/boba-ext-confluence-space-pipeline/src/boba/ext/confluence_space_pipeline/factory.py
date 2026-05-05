"""ConfluenceSpacePipelineFactory: REST → ConfluenceReader → heading → ChromaDB."""

from __future__ import annotations

from typing import Any

from boba.chromadb_store import ChromadbPersistStore
from boba.confluence_reader import ConfluenceReader
from boba.confluence_requests import ConfluenceSpaceRequestSource
from boba.ext.chromadb_shared import ChromadbSharedSection
from boba.ext.confluence_space_pipeline.config import (
    ConfluenceSpacePipelineConfig,
    ConfluenceSpacePipelineConfigSection,
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

__all__ = ["ConfluenceSpacePipelineFactory"]


class ConfluenceSpacePipelineFactory(PipelineFactory):
    """`ext.confluence_space` pipeline."""

    def id(self) -> PipelineId:
        return PipelineId("ext.confluence_space")

    def produce(self, ctx: IndexerExtensionContext) -> IndexPipeline[Any]:
        cfg = ctx.config.section(ConfluenceSpacePipelineConfigSection)
        shared = ctx.config.section(ChromadbSharedSection)
        _validate(cfg)

        auth = _build_auth(cfg)
        request_source = ConfluenceSpaceRequestSource(
            base_url=cfg.base_url,
            auth=auth,
            space_key=cfg.space_key,
            body_format=cfg.body_format,
            timeout_sec=cfg.timeout_sec,
        )
        transport = HttpTransport(timeout_sec=cfg.timeout_sec)
        reader = ConfluenceReader()
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


def _validate(cfg: ConfluenceSpacePipelineConfig) -> None:
    if not cfg.base_url:
        msg = (
            "base_url пуст; задайте через env "
            "BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__BASE_URL."
        )
        raise ValueError(msg)
    if not cfg.space_key:
        msg = "space_key пуст; обязательное поле для confluence_space pipeline."
        raise ValueError(msg)
    if not cfg.auth_token:
        msg = (
            "auth_token пуст; задайте через env "
            "BOBA_INDEXER__PIPELINES__CONFLUENCE_SPACE__AUTH_TOKEN."
        )
        raise ValueError(msg)


def _build_auth(cfg: ConfluenceSpacePipelineConfig) -> AuthApplier:
    if cfg.auth_method == "basic":
        if not cfg.auth_user:
            msg = "auth_method=basic требует auth_user (логин)."
            raise ValueError(msg)
        return BasicAuth(user=cfg.auth_user, password=cfg.auth_token)
    return PatAuth(token=cfg.auth_token)
