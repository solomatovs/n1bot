"""ext.confluence_space: индексирует все страницы одного Confluence space'а."""

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
    "ConfluenceSpaceSource",
    "ConfluenceSpaceSourceConfig",
    "ConfluenceSpaceSourceFactory",
    "ConfluenceSpaceSourceSection",
]

_SOURCE_ID = SourceId("ext.confluence_space")


@dataclass(frozen=True)
class ConfluenceSpaceSourceConfig:
    space_key: str = ""


class ConfluenceSpaceSourceSection(ConfigSection[ConfluenceSpaceSourceConfig]):
    namespace: ClassVar[tuple[str, ...]] = (
        "indexer",
        "sources",
        "confluence",
        "space",
    )

    schema: ClassVar[ObjectSchema[ConfluenceSpaceSourceConfig]] = ObjectSchema(
        description="Confluence-space mode: индексирует все страницы space'а.",
        fields=[
            FieldSpec(
                name="space_key",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "Space-key (например 'DOCS'). Обязателен при выборе "
                    "source=ext.confluence_space."
                ),
            ),
        ],
        factory=ConfluenceSpaceSourceConfig,
    )


class ConfluenceSpaceSource(Source):
    """Все страницы одного space'а — type=page."""

    def __init__(self, base_url: str, space_key: str, client_factory) -> None:
        self._base_url = base_url
        self._space_key = space_key
        self._client_factory = client_factory

    def name(self) -> str:
        return f"ConfluenceSpaceSource({self._space_key})"

    def source_factory_id(self) -> SourceId:
        return _SOURCE_ID

    def stream(self, ctx: IndexingContext) -> Iterable[SourceItem]:
        del ctx
        self._require_space_key()
        with self._client_factory() as client:
            for page in client.pages_in_space(self._space_key):
                yield page_to_item(self._base_url, page)

    def list_source_ids(self) -> Iterable[str]:
        self._require_space_key()
        with self._client_factory() as client:
            for page_id in client.page_ids_in_space(self._space_key):
                yield page_source_id(self._base_url, page_id)

    def _require_space_key(self) -> None:
        if not self._space_key:
            msg = (
                "ext.confluence_space: [indexer.sources.confluence.space] "
                "space_key обязателен."
            )
            raise ValueError(msg)


class ConfluenceSpaceSourceFactory(SourceFactory):
    def id(self) -> SourceId:
        return _SOURCE_ID

    def produce(self, ctx: IndexerExtensionContext) -> Source:
        common = load_common(ctx)
        cfg = ctx.config.section(ConfluenceSpaceSourceSection)
        return ConfluenceSpaceSource(
            base_url=common.base_url,
            space_key=cfg.space_key,
            client_factory=lambda: build_client(common),
        )
