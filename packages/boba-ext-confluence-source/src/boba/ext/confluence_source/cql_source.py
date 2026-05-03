"""ext.confluence_cql: индексирует страницы по CQL-запросу.

CQL = Confluence Query Language. Примеры:
  space = DOCS AND lastModified >= '2024-01-01'
  space in (DOCS, ARCH) AND label = 'kb'
  type = page AND title ~ 'руководство'
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_source._source_base import (
    build_client,
    load_common,
    page_source_id,
    page_to_item,
)
from boba.indexing import (
    IndexerExtensionContext,
    IndexingContext,
    Source,
    SourceFactory,
    SourceId,
    SourceItem,
)

__all__ = [
    "ConfluenceCqlSource",
    "ConfluenceCqlSourceConfig",
    "ConfluenceCqlSourceFactory",
    "ConfluenceCqlSourceSection",
]

_SOURCE_ID = SourceId("ext.confluence_cql")


@dataclass(frozen=True)
class ConfluenceCqlSourceConfig:
    cql: str = ""


class ConfluenceCqlSourceSection(ConfigSection[ConfluenceCqlSourceConfig]):
    namespace: ClassVar[tuple[str, ...]] = (
        "indexer", "sources", "confluence", "cql",
    )

    schema: ClassVar[ObjectSchema[ConfluenceCqlSourceConfig]] = ObjectSchema(
        description=(
            "Confluence-cql mode: индексирует страницы по CQL-запросу. "
            "Пример: \"space = DOCS AND lastModified >= '2024-01-01'\"."
        ),
        fields=[
            FieldSpec(
                name="cql",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "CQL-выражение, например: space = DOCS AND label = 'kb'. "
                    "Обязателен при source=ext.confluence_cql."
                ),
            ),
        ],
        factory=ConfluenceCqlSourceConfig,
    )


class ConfluenceCqlSource(Source):
    """Indexer по результатам CQL-запроса (читает только type=page)."""

    def __init__(self, base_url: str, cql: str, client_factory) -> None:
        self._base_url = base_url
        self._cql = cql
        self._client_factory = client_factory

    def name(self) -> str:
        return "ConfluenceCqlSource"

    def source_factory_id(self) -> SourceId:
        return _SOURCE_ID

    def stream(self, ctx: IndexingContext) -> Iterable[SourceItem]:
        del ctx
        self._require_cql()
        with self._client_factory() as client:
            for page in client.pages_by_cql(self._cql):
                yield page_to_item(self._base_url, page)

    def list_source_ids(self) -> Iterable[str]:
        # CQL-результаты — динамические; при list_source_ids делаем тот же
        # запрос и собираем ids. Полный sync-цикл за два прохода неэффективен,
        # но корректен.
        self._require_cql()
        with self._client_factory() as client:
            for page in client.pages_by_cql(self._cql):
                yield page_source_id(self._base_url, page.page_id)

    def _require_cql(self) -> None:
        if not self._cql:
            msg = (
                "ext.confluence_cql: [indexer.sources.confluence.cql] "
                "cql обязателен."
            )
            raise ValueError(msg)


class ConfluenceCqlSourceFactory(SourceFactory):
    def id(self) -> SourceId:
        return _SOURCE_ID

    def produce(self, ctx: IndexerExtensionContext) -> Source:
        common = load_common(ctx)
        cfg = ctx.config.section(ConfluenceCqlSourceSection)
        return ConfluenceCqlSource(
            base_url=common.base_url,
            cql=cfg.cql,
            client_factory=lambda: build_client(common),
        )
