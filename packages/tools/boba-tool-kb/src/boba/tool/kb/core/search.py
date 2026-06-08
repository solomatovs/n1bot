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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.core.kb import PostgresKnowledgeBase, PostgresKnowledgeBaseConfig
from boba.tool.kb.core.models import KnowledgeBaseError, SearchHit
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = [
    "CollectionSearch",
    "ConfluenceCollection",
    "KbDocCollection",
    "MetaField",
    "kb_doc_fts_search",
    "kb_doc_vector_search",
    "kb_fts_search",
    "kb_vector_search",
]

SearchMethod = Literal["vector", "fts"]


@dataclass(frozen=True)
class MetaField:
    """Output-колонка ← ключ kb_chunks.metadata (та MetadataKey, что у ingest)."""

    column: str
    key: MetadataKey[str]


class CollectionSearch:
    """База дискриминатора: строгий COLLECTION + явная сборка строки из kb_chunks.

    Подкласс обязан задать COLLECTION (scope) и META_FIELDS (поля из
    metadata). Прямые колонки kb_chunks собираются здесь, в row().
    """

    COLLECTION: ClassVar[str]
    META_FIELDS: ClassVar[tuple[MetaField, ...]]

    @classmethod
    def row(cls, hit: SearchHit) -> dict[str, Any]:
        """Плоская строка результата: прямые колонки kb_chunks + поля metadata."""
        row: dict[str, Any] = {
            # --- прямые колонки kb_chunks ---
            "id": hit.id,  # kb_chunks.chunk_id
            "distance": hit.distance,  # cosine / -ts_rank_cd
            "snippet": hit.snippet,  # kb_chunks.format_content (срез)
            "tags": ", ".join(sorted(hit.tags)),  # kb_chunks.tags (text[])
        }
        # --- поля из kb_chunks.metadata (jsonb), по META_FIELDS ---
        for field in cls.META_FIELDS:
            row[field.column] = hit.metadata.get(field.key.name, "")
        # --- производное ---
        row["link"] = cls._link(row)
        return row

    @staticmethod
    def _link(row: Mapping[str, str]) -> str:
        """source_url[#anchor] — готовый deep-link из уже собранных колонок."""
        url = row.get("source_url", "")
        if not url:
            return ""
        anchor = row.get("anchor", "")
        if anchor and "#" not in url:
            return f"{url}#{anchor}"
        return url


class ConfluenceCollection(CollectionSearch):
    """Коллекция Confluence-страниц (прямой HTTP-ingest).

    META_FIELDS ссылается на ключи, которые пишет confluence-ingest:
    request_source (ConfluenceKeys.SOURCE_URL/PAGE_ID), decoder
    (ConfluenceKeys.SPACE_KEY, ReaderKeys.PAGE_TITLE), reader
    (SectionKeys.ANCHOR/HEADING_PATH).
    """

    COLLECTION = "kb_confluence"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),  # decoder
        MetaField("source_url", ConfluenceKeys.SOURCE_URL),  # request_source
        MetaField("anchor", SectionKeys.ANCHOR),  # reader
        MetaField("page_id", ConfluenceKeys.PAGE_ID),  # request_source
        MetaField("heading_path", SectionKeys.HEADING_PATH),  # reader
        MetaField("space", ConfluenceKeys.SPACE_KEY),  # decoder
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
        MetaField("page_title", ReaderKeys.PAGE_TITLE),  # KbDocReader (title:)
        MetaField("source_url", KbDocKeys.SOURCE_URL),  # KbDocReader (source:)
        MetaField("anchor", SectionKeys.ANCHOR),  # KbDocReader (anchor:)
        MetaField("page_id", KbDocKeys.PAGE_ID),  # KbDocReader (page_id:)
        MetaField("heading_path", SectionKeys.HEADING_PATH),  # StructuralChunker
        MetaField("space", KbDocKeys.SPACE),  # KbDocReader (space:)
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
    ) -> TableResult:
        """method выбирает канал, collection — scope и сборку строк."""
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
            raise RuntimeError(str(e)) from e
        return TableResult(rows=rows, note=None if rows else "ничего не найдено")


@tool
def kb_vector_search(
    cfg: Annotated[PostgresKnowledgeBaseConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=KbSearch.QUERY_DESC_VECTOR)],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=KbSearch.SNIPPET_DESC),
    ] = KbSearch.SNIPPET_DEFAULT,
) -> TableResult:
    """Семантический (vector) поиск по коллекции Confluence-страниц (kb_confluence).

    Возвращает TableResult — плоскую таблицу hits с колонками id/distance/
    link/snippet + page_title/source_url/anchor/page_id/
    heading_path/space/tags, по релевантности (меньше distance = ближе).
    """
    return KbSearch.run(
        cfg,
        ConfluenceCollection,
        query,
        "vector",
        top_k,
        snippet_chars,
    )


@tool
def kb_fts_search(
    cfg: Annotated[PostgresKnowledgeBaseConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=KbSearch.QUERY_DESC_FTS)],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=KbSearch.SNIPPET_DESC),
    ] = KbSearch.SNIPPET_DEFAULT,
) -> TableResult:
    """Лексический (FTS) поиск по коллекции Confluence-страниц (kb_confluence).

    Тот же формат TableResult, что и у kb_vector_search.
    """
    return KbSearch.run(cfg, ConfluenceCollection, query, "fts", top_k, snippet_chars)


@tool
def kb_doc_vector_search(
    cfg: Annotated[PostgresKnowledgeBaseConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=KbSearch.QUERY_DESC_VECTOR)],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=KbSearch.SNIPPET_DESC),
    ] = KbSearch.SNIPPET_DEFAULT,
) -> TableResult:
    """Семантический (vector) поиск по KbDoc-выгрузкам Confluence (kb_confluence_doc).

    Тот же формат TableResult, что и у kb_vector_search.
    """
    return KbSearch.run(cfg, KbDocCollection, query, "vector", top_k, snippet_chars)


@tool
def kb_doc_fts_search(
    cfg: Annotated[PostgresKnowledgeBaseConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=KbSearch.QUERY_DESC_FTS)],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=KbSearch.SNIPPET_DESC),
    ] = KbSearch.SNIPPET_DEFAULT,
) -> TableResult:
    """Лексический (FTS) поиск по KbDoc-выгрузкам Confluence (kb_confluence_doc).

    Тот же формат TableResult, что и у kb_vector_search.
    """
    return KbSearch.run(cfg, KbDocCollection, query, "fts", top_k, snippet_chars)
