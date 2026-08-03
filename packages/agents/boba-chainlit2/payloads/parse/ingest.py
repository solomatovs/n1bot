"""Индексация Confluence целиком внутри песочницы.

Пайплайн тот же, что у приложения (ConfluenceIngest из site-packages), но
ридеры локальные: вложенной песочницы внутри песочницы быть не может, а
liteparse и bs4 здесь и так под рукой. Модель эмбеддера грузится один раз на
весь прогон, потому что весь ingest — один запуск payload'а.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar

from pages import PageOps

from boba.chainlit2.agent.tools.confluence.connection import ConfluenceConnection
from boba.chainlit2.agent.tools.confluence.ingest_base import (
    ConfluenceIngest,
    ConfluenceIngestConfig,
)
from boba.chainlit2.agent.tools.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
)
from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
)
from boba.liteparse import LiteParseReader


class LocalConfluenceReader(Reader[str]):
    """HTML-страница -> секции по заголовкам; bs4 работает прямо здесь."""

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")
    DOC_TYPE: ClassVar[str] = "confluence_html"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def read(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        html = payload.decode("utf-8", errors="replace")
        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""
        answer = PageOps.confluence_sections({"html": html, "title": title})
        for row in answer["sections"]:
            yield Section(
                source_id=value.source_id,
                content=row["content"],
                order=row["order"],
                metadata=self._meta(value, row),
            )

    @classmethod
    def _meta(cls, value: RawDocument, row: dict[str, Any]):
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, cls.DOC_TYPE)
        if row["heading_path"]:
            meta = meta.set(SectionKeys.HEADING_PATH, row["heading_path"])
        if row["anchor"]:
            meta = meta.set(SectionKeys.ANCHOR, row["anchor"])
        return meta


class IngestOps:
    """Запуск индексации; вызывается диспетчером payload'а."""

    OPS: ClassVar[tuple[str, ...]] = ("confluence_ingest",)

    @classmethod
    def dispatch(cls, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        if op == "confluence_ingest":
            return cls.ingest(request)
        msg = f"unknown ingest op: {op!r}"
        raise ValueError(msg)

    @classmethod
    def ingest(cls, request: dict[str, Any]) -> dict[str, Any]:
        cfg = ConfluenceIngestConfig.model_validate(request["config"])
        conn = ConfluenceConnection(
            profile=cfg.confluence, body_format=cfg.body_format
        )
        source = cls.source(request, conn, cfg)
        stats = ConfluenceIngest.ingest(
            cfg,
            source,
            request["prune_missing"],
            request["force_update"],
            routes=cls.routes(cfg),
        )
        return {"stats": stats}

    @staticmethod
    def source(request: dict[str, Any], conn: ConfluenceConnection, cfg: Any) -> Any:
        mode = request["mode"]
        if mode == "pages":
            return ConfluencePagesRequestSource(
                base_url=conn.base_url,
                page_ids=list(request["page_ids"]),
                body_format=conn.body_format,
            )
        if mode == "cql":
            return ConfluenceCqlRequestSource(
                conn=conn, cql=request["cql"], body_format=conn.body_format
            )
        if mode == "spaces":
            return ConfluenceMultiSpaceRequestSource(
                conn=conn,
                space_keys=list(request["space_keys"]),
                body_format=conn.body_format,
            )
        msg = f"unknown discovery mode: {mode!r}"
        raise ValueError(msg)

    @staticmethod
    def routes(cfg: Any) -> dict[str, Reader[str]]:
        """HTML читает bs4-ридер, вложения — liteparse; оба локальные."""
        documents = LiteParseReader(cfg)
        routes: dict[str, Reader[str]] = {}
        for content_type in ConfluenceIngest.HTML_CONTENT_TYPES:
            routes[content_type] = LocalConfluenceReader()
        for media_type in documents.media_types:
            routes[media_type] = documents
        return routes
