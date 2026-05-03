"""ext.confluence_pages: индексирует явный список page-id'ов."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ParseCsvList
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
    "ConfluencePagesSource",
    "ConfluencePagesSourceConfig",
    "ConfluencePagesSourceFactory",
    "ConfluencePagesSourceSection",
]

_SOURCE_ID = SourceId("ext.confluence_pages")


@dataclass(frozen=True)
class ConfluencePagesSourceConfig:
    page_ids: list[str] = field(default_factory=list)


class ConfluencePagesSourceSection(ConfigSection[ConfluencePagesSourceConfig]):
    namespace: ClassVar[tuple[str, ...]] = (
        "indexer", "sources", "confluence", "pages",
    )

    schema: ClassVar[ObjectSchema[ConfluencePagesSourceConfig]] = ObjectSchema(
        description="Confluence-pages mode: индексирует явный список page-id'ов.",
        fields=[
            FieldSpec(
                name="page_ids",
                coercer=ParseCsvList(),
                description=(
                    "Список Confluence page-id'ов (CSV в env, TOML-array). "
                    "Обязателен при source=ext.confluence_pages."
                ),
            ),
        ],
        factory=ConfluencePagesSourceConfig,
    )


class ConfluencePagesSource(Source):
    """Indexer поверх явного списка page-id'ов."""

    def __init__(self, base_url: str, page_ids: list[str], client_factory) -> None:
        self._base_url = base_url
        self._page_ids = list(page_ids)
        self._client_factory = client_factory

    def name(self) -> str:
        return f"ConfluencePagesSource(n={len(self._page_ids)})"

    def source_factory_id(self) -> SourceId:
        return _SOURCE_ID

    def stream(self, ctx: IndexingContext) -> Iterable[SourceItem]:
        del ctx
        self._require_page_ids()
        with self._client_factory() as client:
            for pid in self._page_ids:
                page = client.page_by_id(pid)
                yield page_to_item(self._base_url, page)

    def list_source_ids(self) -> Iterable[str]:
        self._require_page_ids()
        for pid in self._page_ids:
            yield page_source_id(self._base_url, pid)

    def _require_page_ids(self) -> None:
        if not self._page_ids:
            msg = (
                "ext.confluence_pages: [indexer.sources.confluence.pages] "
                "page_ids обязателен (непустой список)."
            )
            raise ValueError(msg)


class ConfluencePagesSourceFactory(SourceFactory):
    def id(self) -> SourceId:
        return _SOURCE_ID

    def produce(self, ctx: IndexerExtensionContext) -> Source:
        common = load_common(ctx)
        cfg = ctx.config.section(ConfluencePagesSourceSection)
        return ConfluencePagesSource(
            base_url=common.base_url,
            page_ids=cfg.page_ids,
            client_factory=lambda: build_client(common),
        )
