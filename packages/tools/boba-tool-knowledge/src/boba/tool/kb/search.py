"""KB search tools (vector/fts): дискриминатор коллекции строит строку kb_chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.caller import KbCaller
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tool.kb.models import KnowledgeBaseError, SearchHit
from boba.toolkit.result import (
    ErrorResult,
    TableResult,
    ToolResult,
)

__all__ = [
    "CollectionSearch",
    "ConfluenceCollection",
    "KbDocCollection",
    "KbSearch",
    "MetaField",
]

SearchMethod = Literal["vector", "fts"]


@dataclass(frozen=True)
class MetaField:
    """Output-колонка ← ключ kb_chunks.metadata (та же MetadataKey, что у ingest)."""

    column: str
    key: MetadataKey[Any]


class CollectionSearch:
    """База дискриминатора: подкласс задаёт COLLECTION и META_FIELDS, строку собирает
    row().
    """

    COLLECTION: ClassVar[str]
    META_FIELDS: ClassVar[tuple[MetaField, ...]]

    @classmethod
    def row(cls, hit: SearchHit) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": hit.id,
            "distance": hit.distance,
            "snippet": hit.snippet,
            "tags": ", ".join(sorted(hit.tags)),
        }
        for field in cls.META_FIELDS:
            row[field.column] = hit.metadata.get(field.key.name, "")
        return row


class ConfluenceCollection(CollectionSearch):
    """Коллекция Confluence-страниц: META_FIELDS — ключи, которые пишет confluence-
    ingest.
    """

    COLLECTION = "kb_confluence"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),
        MetaField("source_url", ConfluenceKeys.SOURCE_URL),
        MetaField("parent_url", ConfluenceKeys.PARENT_URL),
        MetaField("doc_type", ReaderKeys.DOC_TYPE),
        MetaField("page", SectionKeys.PAGE_NUMBER),
        MetaField("anchor", SectionKeys.ANCHOR),
        MetaField("page_id", ConfluenceKeys.PAGE_ID),
        MetaField("version", ConfluenceKeys.VERSION),
        MetaField("heading_path", SectionKeys.HEADING_PATH),
        MetaField("space", ConfluenceKeys.SPACE_KEY),
    )


class KbDocCollection(CollectionSearch):
    """Коллекция KbDoc-выгрузок из workspace; wire-имена совпадают с confluence-
    коллекцией.
    """

    COLLECTION = "kb_confluence_doc"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),
        MetaField("source_url", KbDocKeys.SOURCE_URL),
        MetaField("parent_url", KbDocKeys.PARENT_URL),
        MetaField("doc_type", ReaderKeys.DOC_TYPE),
        MetaField("page", SectionKeys.PAGE_NUMBER),
        MetaField("anchor", SectionKeys.ANCHOR),
        MetaField("page_id", KbDocKeys.PAGE_ID),
        MetaField("version", KbDocKeys.VERSION),
        MetaField("heading_path", SectionKeys.HEADING_PATH),
        MetaField("space", KbDocKeys.SPACE),
    )


class KbSearch:
    """Единый прогон KB-поиска: method выбирает канал, collection — scope."""

    VECTOR_SQL: ClassVar[str] = """
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) as snippet,
    (c.embedding::vector({dim})) <=> %(embedding)s::vector as distance
from
    {chunks_table} c
where
    c.collection = any(%(collections)s)
    and c.embedding is not null
order by
    distance asc
limit
    %(top_k)s
"""
    """Векторный поиск: format-плейсхолдеры {dim}/{chunks_table}, остальное — bind."""

    FTS_SQL: ClassVar[str] = """
with q as (
    select
        websearch_to_tsquery('russian', {schema}.immutable_unaccent(%(query)s))
        || websearch_to_tsquery('english', {schema}.immutable_unaccent(%(query)s))
        as tsq
)
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) as snippet,
    ts_rank_cd(c.tsv, q.tsq) as rank
from
    {chunks_table} c,
    q
where
    c.collection = any(%(collections)s)
    and c.tsv @@ q.tsq
order by
    rank desc
limit
    %(top_k)s
"""
    """FTS-поиск: format-плейсхолдеры {chunks_table}/{schema}, остальное — bind."""

    QUERY_DESC_VECTOR: ClassVar[str] = (
        "Поисковый запрос на естественном языке — семантический поиск "
        "(cosine-эмбеддинги: ловит синонимы и перефразировки, хорош для "
        "длинных/размытых формулировок)."
    )
    QUERY_DESC_FTS: ClassVar[str] = (
        "Поисковый запрос в websearch-синтаксисе — лексический поиск "
        "(`ts_rank_cd`, точные слова/имена/идентификаторы): пробел = AND, "
        '`OR` = альтернативы, `"фраза"` = фраза целиком, `-слово` = исключить.'
    )
    TOPK_DESC: ClassVar[str] = "Сколько hits вернуть. По умолчанию 5."
    SNIPPET_DEFAULT: ClassVar[int] = 3000
    SNIPPET_DESC: ClassVar[str] = (
        "Максимальная длина сниппета документа в hits (символов). По умолчанию 3000."
    )

    @staticmethod
    def run(  # noqa: PLR0913
        cfg: PostgresKnowledgeBaseConfig,
        caller: KbCaller,
        collection: type[CollectionSearch],
        query: str,
        method: SearchMethod,
        top_k: int,
        snippet_chars: int,
    ) -> ToolResult:
        kb = PostgresKnowledgeBase(cfg=cfg, caller=caller)
        try:
            if method == "vector":
                hits = kb.vector_search(
                    collections=[collection.COLLECTION],
                    query=query,
                    top_k=top_k,
                    snippet_chars=snippet_chars,
                    sql_template=KbSearch.VECTOR_SQL,
                )
            else:
                hits = kb.fts_search(
                    collections=[collection.COLLECTION],
                    query=query,
                    top_k=top_k,
                    snippet_chars=snippet_chars,
                    sql_template=KbSearch.FTS_SQL,
                )
            rows = [collection.row(h) for h in hits]
        except KnowledgeBaseError as e:
            return ErrorResult(message=str(e), error_kind="kb_search_failed")
        return TableResult(rows=rows, note=None if rows else "ничего не найдено")
