"""Tools `kb_confluence_search` / `kb_confluence_doc_search` + дискриминаторы коллекций.

Единый интерфейс: параметр `method` (`vector` | `fts`) выбирает канал поиска.
Каждый tool строго привязан к ОДНОЙ коллекции через дискриминирующий тип
(`ConfluenceCollection` / `KbDocCollection`): строгий фильтр по `collection`
даёт детерминированную сборку строки из `kb_chunks` (`CollectionSearch.row`),
без `if/else`. Каждый тип ссылается на те же `MetadataKey`-константы, что
пишет ingest этой коллекции — так search-сторона ↔ ingest-сторона не расходятся.

Общая конфиг-секция: `[tool.kb.search]` (knowledge_base / max_top_k / пути к
SQL-шаблонам vector|fts).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.models import ConfluenceKeys
from boba.tool.kb.core.kb import PostgresKnowledgeBase, PostgresKnowledgeBaseConfig
from boba.tool.kb.core.models import KnowledgeBaseError, SearchHit
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = [
    "CollectionSearch",
    "ConfluenceCollection",
    "KbDocCollection",
    "KbSearchConfig",
    "MetaField",
    "kb_confluence_doc_search",
    "kb_confluence_search",
]

SearchMethod = Literal["vector", "fts"]


@dataclass(frozen=True)
class MetaField:
    """Output-колонка ← ключ `kb_chunks.metadata` (та `MetadataKey`, что у ingest)."""

    column: str
    key: MetadataKey[str]


class CollectionSearch:
    """База дискриминатора: строгий `COLLECTION` + явная сборка строки из kb_chunks.

    Подкласс обязан задать `COLLECTION` (scope) и `META_FIELDS` (поля из
    `metadata`). Прямые колонки `kb_chunks` собираются здесь, в `row()`.
    """

    COLLECTION: ClassVar[str]
    META_FIELDS: ClassVar[tuple[MetaField, ...]]

    @classmethod
    def row(cls, hit: SearchHit) -> dict[str, Any]:
        """Плоская строка результата: прямые колонки kb_chunks + поля metadata."""
        row: dict[str, Any] = {
            # --- прямые колонки kb_chunks ---
            "id": hit.id,                          # kb_chunks.chunk_id
            "distance": hit.distance,              # cosine / -ts_rank_cd
            "snippet": hit.snippet,                # kb_chunks.format_content (срез)
            "tags": ", ".join(sorted(hit.tags)),   # kb_chunks.tags (text[])
        }
        # --- поля из kb_chunks.metadata (jsonb), по META_FIELDS ---
        for field in cls.META_FIELDS:
            row[field.column] = hit.metadata.get(field.key.name, "")
        # --- производное ---
        row["link"] = cls._link(row)
        return row

    @staticmethod
    def _link(row: Mapping[str, str]) -> str:
        """`source_url[#anchor]` — готовый deep-link из уже собранных колонок."""
        url = row.get("source_url", "")
        if not url:
            return ""
        anchor = row.get("anchor", "")
        if anchor and "#" not in url:
            return f"{url}#{anchor}"
        return url


class ConfluenceCollection(CollectionSearch):
    """Коллекция Confluence-страниц (прямой HTTP-ingest).

    `META_FIELDS` ссылается на ключи, которые пишет confluence-ingest:
    request_source (`ConfluenceKeys.SOURCE_URL`/`PAGE_ID`), decoder
    (`ConfluenceKeys.SPACE_KEY`, `ReaderKeys.PAGE_TITLE`), reader
    (`SectionKeys.ANCHOR`/`HEADING_PATH`).
    """

    COLLECTION = "kb_confluence"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),       # decoder
        MetaField("source_url", ConfluenceKeys.SOURCE_URL),   # request_source
        MetaField("anchor", SectionKeys.ANCHOR),              # reader
        MetaField("page_id", ConfluenceKeys.PAGE_ID),         # request_source
        MetaField("heading_path", SectionKeys.HEADING_PATH),  # reader
        MetaField("space", ConfluenceKeys.SPACE_KEY),         # decoder
    )


class KbDocCollection(CollectionSearch):
    """Коллекция загруженных KbDoc-выгрузок Confluence (workspace upload).

    `META_FIELDS` ссылается на ключи, которые пишет kbdoc-ingest: `KbDocReader`
    из header'а (`KbDocKeys.SOURCE_URL`/`PAGE_ID`/`SPACE`, `ReaderKeys.
    PAGE_TITLE`, `SectionKeys.ANCHOR`) + `StructuralChunker`
    (`SectionKeys.HEADING_PATH`). Wire-имена совпадают с confluence-коллекцией.
    """

    COLLECTION = "kb_confluence_doc"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),       # KbDocReader (title:)
        MetaField("source_url", KbDocKeys.SOURCE_URL),        # KbDocReader (source:)
        MetaField("anchor", SectionKeys.ANCHOR),              # KbDocReader (anchor:)
        MetaField("page_id", KbDocKeys.PAGE_ID),              # KbDocReader (page_id:)
        MetaField("heading_path", SectionKeys.HEADING_PATH),  # StructuralChunker
        MetaField("space", KbDocKeys.SPACE),                  # KbDocReader (space:)
    )


class KbSearch:
    """Единый прогон KB-поиска: `method` выбирает канал, `collection` — scope."""

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
    """Pure vector retrieval: top-K по cosine-distance (pgvector `<=>`).

    Identifier-плейсхолдеры (`sql.SQL.format`): `{dim}`, `{chunks_table}`.
    Bind-параметры (named-style): `collections`/`embedding`/`snippet_chars`/`top_k`.
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
    """Pure FTS retrieval: top-K по `ts_rank_cd` без vector-канала.

    Identifier-плейсхолдеры (`sql.SQL.format`): `{chunks_table}`, `{schema}`.
    Bind-параметры (named-style): `collections`/`query`/`snippet_chars`/`top_k`.

    Multilang FTS: tsquery строится как `russian || english` — совпадает с
    хранимым tsv из миграции `002_multilang_tsv.sql` (набор языков должен быть
    синхронен с DDL tsv-колонки). `websearch_to_tsquery` (не `plainto_`): пробел
    = AND, но LLM может сама управлять (`OR`, `"фраза"`, `-исключение`); функция
    тотальна — экранировать ничего не нужно.
    """

    QUERY_DESC: ClassVar[str] = (
        "Поисковый запрос. Для `method=vector` — естественный язык (семантика). "
        'Для `method=fts` — websearch-синтаксис: пробел = AND, `OR` = альтернативы, '
        '`"фраза"` = фраза целиком, `-слово` = исключить.'
    )
    METHOD_DESC: ClassVar[str] = (
        "Канал поиска: `vector` — семантический (cosine-эмбеддинги, синонимы, "
        "длинные запросы); `fts` — лексический (`ts_rank_cd`, точные слова/имена/"
        "идентификаторы). По умолчанию `vector`."
    )
    TOPK_DESC: ClassVar[str] = "Сколько hits вернуть. По умолчанию 5."

    @staticmethod
    def run(
        cfg: KbSearchConfig,
        collection: type[CollectionSearch],
        query: str,
        method: SearchMethod,
        top_k: int,
    ) -> TableResult:
        """`method` выбирает канал, `collection` — scope и сборку строк."""
        if top_k > cfg.max_top_k:
            raise RuntimeError(f"top_k={top_k} превышает max_top_k={cfg.max_top_k}")
        kb = PostgresKnowledgeBase(cfg=cfg.knowledge_base)
        try:
            if method == "vector":
                hits = kb.vector_search(
                    collections=[collection.COLLECTION],
                    query=query,
                    top_k=top_k,
                    sql_template=KbSearch.VECTOR_SQL,
                )
            else:
                hits = kb.fts_search(
                    collections=[collection.COLLECTION],
                    query=query,
                    top_k=top_k,
                    sql_template=KbSearch.FTS_SQL,
                )
            rows = [collection.row(h) for h in hits]
        except KnowledgeBaseError as e:
            raise RuntimeError(str(e)) from e
        return TableResult(rows=rows, note=None if rows else "ничего не найдено")


class KbSearchConfig(BobaFlatSettings):
    """Общий конфиг search-tool'ов. Секция `[tool.kb.search]`."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
        ),
    )

    knowledge_base: PostgresKnowledgeBaseConfig
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра `top_k`.",
    )


@tool
def kb_confluence_search(
    cfg: Annotated[KbSearchConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=KbSearch.QUERY_DESC)],
    method: Annotated[SearchMethod, Field(description=KbSearch.METHOD_DESC)] = "vector",
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
) -> TableResult:
    """Поиск по коллекции Confluence-страниц (`kb_confluence`).

    Канал выбирается параметром `method` (`vector`|`fts`). Возвращает
    `TableResult` — плоскую таблицу hits с колонками `id`/`distance`/`link`/
    `snippet` + `page_title`/`source_url`/`anchor`/`page_id`/`heading_path`/
    `space`/`tags`, по релевантности (меньше `distance` = ближе).
    """
    return KbSearch.run(cfg, ConfluenceCollection, query, method, top_k)


@tool
def kb_confluence_doc_search(
    cfg: Annotated[KbSearchConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=KbSearch.QUERY_DESC)],
    method: Annotated[SearchMethod, Field(description=KbSearch.METHOD_DESC)] = "vector",
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
) -> TableResult:
    """Поиск по коллекции загруженных KbDoc-выгрузок Confluence (`kb_confluence_doc`).

    Канал выбирается параметром `method` (`vector`|`fts`). Тот же формат
    `TableResult`, что и у `kb_confluence_search`.
    """
    return KbSearch.run(cfg, KbDocCollection, query, method, top_k)
