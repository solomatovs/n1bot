"""Tools `confluence_ingest_{spaces,pages,cql}` + `ConfluenceIngestConfig`.

Три отдельных tool'а (по одному на режим discovery) поверх общего pipeline'а
(`ConfluenceIngest`):

- `confluence_ingest_spaces(space_keys=[A, B])`
    → все страницы перечисленных Confluence-spaces
      (discovery `/rest/api/space/{key}/content`).
- `confluence_ingest_pages(page_ids=[123, 456])`
    → явный список страниц по ID.
- `confluence_ingest_cql(cql="space = DOCS AND ...")`
    → discovery страниц через CQL.

Один tool = одно намерение: каждая функция принимает РОВНО ОДИН
discovery-параметр (без `anyOf`/`Optional`), что упрощает grammar
constrained decoding у LLM. Все три используют **одну** конфиг-секцию
`[tool.kb.confluence.ingest]` (store/embedding/chunker/confluence/collection).

Pipeline: `RequestSource` → `ConfluenceContentTransport` (HTTP + JSON-decode +
attachment fan-out) → `DispatchReader` по `CONTENT_TYPE` (HTML →
`ConfluenceReader`, прочее → skip) → `StructuralChunker` →
`CollectionScopedView` → `PostgresChunkStore`. Вложения (PDF/docx, прошедшие
`AttachmentFilter`) индексируются как отдельные чанки с `source_id` = URL
вложения и теми же `confluence.*` ключами родительской страницы. Набор
metadata-ключей — `boba.tool.kb.confluence.models`.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from pydantic import Field

from boba.indexing import (
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    RequestSource,
    StreamingIndexer,
    TransportKeys,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.indexing.reader import ReaderId
from boba.settings import (
    BobaFlatSettings,
    BobaSettingsConfigDict,
    LLMStringList,
    StringList,
)
from boba.text import StructuralChunker
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import AttachmentFilter
from boba.tool.kb.confluence.pipeline import ConfluenceContentTransport
from boba.tool.kb.confluence.reading import ConfluenceReader
from boba.tool.kb.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
)
from boba.tool.kb.core.chunking import ChunkerParams
from boba.tool.kb.core.embedding import EmbeddingModel
from boba.tool.kb.core.postgres import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tools import FromConfig, tool
from boba.transport.http import HttpRequest

__all__ = [
    "ConfluenceIngestConfig",
    "confluence_ingest_cql",
    "confluence_ingest_pages",
    "confluence_ingest_spaces",
]


class ConfluenceIngestConfig(BobaFlatSettings):
    """Self-contained конфиг семейства tool'ов `confluence_ingest_*`.

    Config-секция: `[tool.kb.confluence.ingest]`. Делится между всеми тремя
    режимами (spaces / pages / cql).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.ingest",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
            "confluence",
        ),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    confluence: ConfluenceConnection
    collection: str = Field(
        default="kb_confluence",
        min_length=1,
        max_length=255,
        description="Target-коллекция в `kb_chunks`.",
    )
    attachment_media_types: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.media_type` "
                "(напр. `application/pdf`, `image/*`). Если пусто И "
                "`attachment_titles` пуст — пропускаются ВСЕ вложения "
                "(старое поведение). Если задано — attachment проходит, "
                "если матчит хоть один паттерн в любом из двух списков (OR)."
            ),
        ),
    ] = []  # noqa: RUF012
    attachment_titles: Annotated[
        StringList,
        Field(
            description=(
                "Allowlist fnmatch-globs для `attachment.title` "
                "(напр. `*.pdf`, `report-*.docx`). См. `attachment_media_types`."
            ),
        ),
    ] = []  # noqa: RUF012


class ConfluenceIngest:
    """Сборка Confluence-ingest pipeline — общий хвост для `confluence_ingest_*`."""

    HTML_CONTENT_TYPES: ClassVar[tuple[str, ...]] = ("text/html",)
    """CONTENT_TYPE-значения, которые `ConfluenceJsonDecoder` ставит после
    распаковки JSON→HTML; всё, что попадает под эти ключи, идёт в HTML-Reader."""

    PIPELINE_ID_SPACES: ClassVar[PipelineId] = PipelineId("kb.confluence_ingest_spaces")
    PIPELINE_ID_PAGES: ClassVar[PipelineId] = PipelineId("kb.confluence_ingest_pages")
    PIPELINE_ID_CQL: ClassVar[PipelineId] = PipelineId("kb.confluence_ingest_cql")

    @staticmethod
    def run(  # noqa: PLR0913 — keyword-only helper, явный набор deps
        *,
        request_source: RequestSource[HttpRequest],
        conn: ConfluenceConnection,
        chunk_store: PostgresChunkStore,
        collections_store: PostgresCollectionsStore,
        embedder: Embedder[str],
        chunker: StructuralChunker,
        collection: str,
        prune_missing: bool,
        pipeline_id: PipelineId,
        attachment_filter: AttachmentFilter | None = None,
    ) -> dict[str, Any]:
        """Полный Confluence → kb_chunks pipeline для уже-собранного `RequestSource`.

        Возвращает JSON-stats с полями collection/indexed/skipped_unchanged/
        pruned/failed. Caller добавляет свои поля (space_key/page_ids/...).

        Reader — `DispatchReader` по `TransportKeys.CONTENT_TYPE`: HTML
        обрабатывается `ConfluenceReader`'ом, всё остальное (attachment'ы PDF/
        image/etc.) молча пропускается. Когда появятся Reader'ы для PDF или
        картинок, их можно подключить добавив entry в `routes`-mapping.
        """
        confluence_reader = ConfluenceReader()
        reader: DispatchReader[str] = DispatchReader(
            by=TransportKeys.CONTENT_TYPE,
            routes=dict.fromkeys(
                ConfluenceIngest.HTML_CONTENT_TYPES, confluence_reader,
            ),
            reader_id=ReaderId("ext.confluence_dispatch"),
            on_unknown="skip",
        )

        collection_id = CollectionId(collection)
        collections_store.ensure_collection(collection_id, description=None)

        view: CollectionScopedView[str] = CollectionScopedView(
            store=chunk_store,
            embedder=embedder,
            collection=collection_id,
        )
        indexer: StreamingIndexer[HttpRequest, str] = StreamingIndexer(
            request_source=request_source,
            transport=ConfluenceContentTransport.from_connection(
                conn, attachment_filter=attachment_filter,
            ),
            decoders=(),
            reader=reader,
            chunker=chunker,
            sink=view,
            query=view,
        )
        config: IndexerConfig[str] = IndexerConfig(
            cleanup=FullCleanup() if prune_missing else NoneCleanup(),
            force_update=False,
        )
        stats = indexer.invoke(PipelineContext(pipeline_id=pipeline_id), config)

        return {
            "collection": str(collection_id),
            "indexed": stats.chunks_upserted,
            "skipped_unchanged": stats.sources_skipped_unchanged,
            "pruned": stats.chunks_deleted,
            "failed": stats.sources_failed,
        }

    @staticmethod
    def _run(
        cfg: ConfluenceIngestConfig,
        request_source: RequestSource[HttpRequest],
        prune_missing: bool,
        pipeline_id: PipelineId,
    ) -> dict[str, Any]:
        """Собрать stores/embedder/chunker/filter из cfg и вызвать `run`."""
        chunk_store = PostgresChunkStore(cfg=cfg.store)
        collections_store = PostgresCollectionsStore(cfg=cfg.store)
        embedder = cfg.embedding.build()
        chunker = cfg.chunker.build_chunker()
        att_filter = AttachmentFilter.from_lists(
            media_types=cfg.attachment_media_types,
            titles=cfg.attachment_titles,
        )
        return ConfluenceIngest.run(
            request_source=request_source,
            conn=cfg.confluence,
            chunk_store=chunk_store,
            collections_store=collections_store,
            embedder=embedder,
            chunker=chunker,
            collection=cfg.collection,
            prune_missing=prune_missing,
            pipeline_id=pipeline_id,
            attachment_filter=att_filter,
        )


@tool
def confluence_ingest_spaces(
    cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    space_keys: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список space-ключей Confluence для индексации. "
                'Передавай JSON-массив строк: `["DOCS", "ENG"]`. Discovery '
                "через `/rest/api/space/{key}/content` — индексируются все "
                "страницы каждого space. Используй, когда нужен полный "
                "охват space'а."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди страниц, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует ВСЕ страницы перечисленных Confluence-spaces в KB.

    Возвращает JSON `{collection, indexed, skipped_unchanged, pruned, failed}`.
    """
    request_source = ConfluenceMultiSpaceRequestSource(
        conn=cfg.confluence,
        space_keys=space_keys,
        body_format=cfg.confluence.body_format,
    )
    result = ConfluenceIngest._run(
        cfg, request_source, prune_missing, ConfluenceIngest.PIPELINE_ID_SPACES,
    )
    return {"space_keys": space_keys, **result}


@tool
def confluence_ingest_pages(
    cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    page_ids: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список page_id страниц Confluence для индексации. "
                'Передавай JSON-массив строк: `["950276", "950278"]`. '
                "Каждый id — строка из URL `viewpage.action?pageId=<id>`. "
                "Используй, когда уже знаешь конкретные страницы (например, "
                "из результатов `confluence_search_cql`)."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди страниц, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует явный список страниц Confluence по page_id в KB.

    Возвращает JSON `{collection, indexed, skipped_unchanged, pruned, failed}`.
    """
    request_source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=page_ids,
        body_format=cfg.confluence.body_format,
    )
    result = ConfluenceIngest._run(
        cfg, request_source, prune_missing, ConfluenceIngest.PIPELINE_ID_PAGES,
    )
    return {"page_ids": page_ids, **result}


@tool
def confluence_ingest_cql(
    cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    cql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "CQL-запрос для discovery страниц. Пример: "
                '`space = DOCS AND lastModified > "2024-01-01"`. '
                "Используй для тонких фильтров (по дате, метке, автору) — "
                "когда не нужен весь space, но и конкретных page_id нет."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди страниц, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует страницы Confluence, отобранные CQL-запросом, в KB.

    Возвращает JSON `{collection, indexed, skipped_unchanged, pruned, failed}`.
    """
    request_source = ConfluenceCqlRequestSource(
        conn=cfg.confluence,
        cql=cql,
        body_format=cfg.confluence.body_format,
    )
    result = ConfluenceIngest._run(
        cfg, request_source, prune_missing, ConfluenceIngest.PIPELINE_ID_CQL,
    )
    return {"cql": cql, **result}
