"""KB search tools: 4 штуки = {vector, fts} × {confluence, kb_doc}.

Канал (vector/fts) и коллекция зашиты в сам tool — параметра method нет:
- kb_vector_search / kb_fts_search         — коллекция kb_confluence;
- kb_doc_vector_search / kb_doc_fts_search — коллекция kb_confluence_doc.

Коллекция фиксируется дискриминирующим типом (ConfluenceCollection /
KbDocCollection): строгий фильтр по collection даёт детерминированную
сборку строки из kb_chunks (CollectionSearch.row). Каждый тип ссылается
на те же MetadataKey-константы, что пишет ingest этой коллекции — так
search-сторона ↔ ingest-сторона не расходятся. Все 4 tool'а читают
конфиг-секцию [tool.kb] напрямую как PostgresKnowledgeBaseConfig
(connection/tables/embedding). top_k/snippet_chars — tool-аргументы LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from boba.chainlit2.agent.tools.confluence.models import ConfluenceKeys
from boba.chainlit2.agent.tools.kb.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.chainlit2.agent.tools.kb.models import KnowledgeBaseError, SearchHit
from boba.chainlit2.rendering.tool_result import (
    ErrorResult,
    TableResult,
    ToolResult,
)
from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys

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
    """Output-колонка ← ключ kb_chunks.metadata (та MetadataKey, что у ingest).

    row() читает значение по wire-имени из jsonb (всегда строка), поэтому
    тип-параметр ключа не важен — допускаем любой (напр. VERSION: int).
    """

    column: str
    key: MetadataKey[Any]


class CollectionSearch:
    """База дискриминатора: строгий COLLECTION + явная сборка строки из kb_chunks.

    Подкласс обязан задать COLLECTION (scope) и META_FIELDS (поля из
    metadata). Прямые колонки kb_chunks собираются здесь, в row().
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
    """Коллекция Confluence-страниц (прямой HTTP-ingest).

    META_FIELDS ссылается на ключи, которые пишет confluence-ingest:
    request_source (ConfluenceKeys.SOURCE_URL/PAGE_ID), decoder
    (ConfluenceKeys.SPACE_KEY, ReaderKeys.PAGE_TITLE), reader
    (SectionKeys.ANCHOR/HEADING_PATH).
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
    """Коллекция загруженных KbDoc-выгрузок Confluence (workspace upload).

    META_FIELDS ссылается на ключи, которые пишет kbdoc-ingest: KbDocReader
    из header'а (KbDocKeys.SOURCE_URL/PAGE_ID/SPACE, ReaderKeys.
    PAGE_TITLE, SectionKeys.ANCHOR) + StructuralChunker
    (SectionKeys.HEADING_PATH). Wire-имена совпадают с confluence-коллекцией.
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
    left(c.format_content, %(snippet_chars)s) AS snippet,
    (c.embedding::vector({dim})) <=> %(embedding)s::vector AS distance
from
    {chunks_table} c
where 1=1
    and c.collection = any(%(collections)s)
    and c.embedding is not null
order by
    distance asc
limit
    %(top_k)s
"""
    """Поисковый запрос по векторному индексу

    Identifier-плейсхолдеры (sql.SQL.format): {dim}, {chunks_table}.
    Bind-параметры (named-style): collections/embedding/snippet_chars/top_k.
    """

    FTS_SQL: ClassVar[str] = """
with q as (
    select websearch_to_tsquery('russian', {schema}.immutable_unaccent(%(query)s))
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
where 1=1
    and c.collection = any(%(collections)s)
    and c.tsv @@ q.tsq
order by
    rank desc
limit
    %(top_k)s
"""
    """Поисковый запрос по full-text индексу

    Identifier-плейсхолдеры (sql.SQL.format): {chunks_table}, {schema}.
    Bind-параметры (named-style): collections/query/snippet_chars/top_k.
    """

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
    def run(  # noqa: PLR0913 — явный набор search-параметров
        cfg: PostgresKnowledgeBaseConfig,
        collection: type[CollectionSearch],
        query: str,
        method: SearchMethod,
        top_k: int,
        snippet_chars: int,
    ) -> ToolResult:
        kb = PostgresKnowledgeBase(cfg=cfg)
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
