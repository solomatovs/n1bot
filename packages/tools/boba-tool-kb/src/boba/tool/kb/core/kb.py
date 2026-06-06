"""Read-only KB-обёртка над postgres+pgvector — для search-tools'ов.

`PostgresKnowledgeBase` — тонкий wrapper над connection pool + embedder,
с двумя операциями tool-уровня:

- `vector_search(...)`  — **чистый** semantic top-K от pgvector (cosine
                          via `<=>`). Канал `method=vector` search-tool'ов.
                          Полезен, когда FTS-канал шумит/мешает (короткие
                          запросы, эмбеддинг лучше ловит синонимы).
- `fts_search(...)`     — **чистый** lexical top-K от Postgres FTS
                          (`ts_rank_cd`). Канал `method=fts` search-tool'ов.
                          Полезен для точных лексических совпадений
                          (имена, идентификаторы, фразы).

Snippet обрезан по `snippet_chars`, метадата нормализована под формат
tool'ов.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, LiteralString, cast

from psycopg import sql
from pydantic import BaseModel

from boba.db.postgres import PostgresConnection
from boba.tool.kb.core.embedding import EmbeddingModel
from boba.tool.kb.core.models import KnowledgeBaseError, SearchHit
from boba.tool.kb.core.postgres import KbPool, PostgresStoreSchema

logger = logging.getLogger(__name__)

__all__ = [
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
]


class PostgresKnowledgeBaseConfig(BaseModel):
    """Composite-конфиг для read-side KB.

    Поля: connection + tables + embedding. Язык(и) FTS зашиты в SQL-шаблоны
    (`KbSearch.VECTOR_SQL`/`FTS_SQL` в `core/search.py`) и в DDL `tsv`-колонки
    (см. `migrations/002_multilang_tsv.sql`) — оба места должны быть синхронны.
    """

    connection: PostgresConnection
    tables: PostgresStoreSchema
    embedding: EmbeddingModel


class PostgresKnowledgeBase:
    """
    Доступ к хранилищу kb
    """

    def __init__(
        self,
        *,
        cfg: PostgresKnowledgeBaseConfig,
    ) -> None:
        self._cfg = cfg
        self._pool = KbPool.open(cfg.connection)
        self._embedder = cfg.embedding.build()
        logger.info(
            "PostgresKnowledgeBase opened dim=%d chunks=%s.%s",
            self._embedder.dim(),
            cfg.tables.pg_schema,
            cfg.tables.chunks_table,
        )

    def vector_search(
        self,
        *,
        collections: list[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        sql_template: str,
    ) -> Iterable[SearchHit]:
        """Чистый vector top-K (cosine via `<=>`) без FTS-канала.

        Ищет по объединению переданных коллекций: SQL-фильтр
        `collection = ANY(%(collections)s)`.

        `distance` — cosine-distance pgvector'а (меньше = ближе, диапазон
        [0..2]).

        Snippet режется до `snippet_chars`, метадата нормализуется
        `_row_metadata`.

        `sql_template` — текст SQL с identifier-placeholder'ами
        `{dim}`/`{chunks_table}` и bind-параметрами
        `%(collections|embedding|snippet_chars|top_k)s`. Источник —
        константа `KbSearch.VECTOR_SQL` (`core/search.py`).
        """
        embedding = list(self._embedder.embed_query(query))
        query_sql = sql.SQL(cast(LiteralString, sql_template)).format(
            dim=sql.Literal(self._embedder.dim()),
            chunks_table=self._cfg.tables.chunks_ident(),
        )
        try:
            with self._pool.dict_cursor() as cur:
                cur.execute(
                    query_sql,
                    {
                        "collections": list(collections),
                        "embedding": embedding,
                        "snippet_chars": snippet_chars,
                        "top_k": top_k,
                    },
                )

                for row in cur:
                    yield SearchHit(
                        id=row["chunk_id"],
                        distance=float(row["distance"]),
                        metadata=self._row_metadata(row),
                        snippet=row["snippet"] or "",
                        tags=tuple(row.get("tags") or ()),
                    )
        except Exception as e:
            raise KnowledgeBaseError(
                f"postgres vector search failed for collections "
                f"{list(collections)!r}: {type(e).__name__}: {e}",
            ) from e

    def fts_search(
        self,
        *,
        collections: list[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        sql_template: str,
    ) -> Iterable[SearchHit]:
        """
        FTS serarch top-k
        """
        query_sql = sql.SQL(cast(LiteralString, sql_template)).format(
            chunks_table=self._cfg.tables.chunks_ident(),
            schema=self._cfg.tables.schema_ident(),
        )
        try:
            with self._pool.dict_cursor() as cur:
                cur.execute(
                    query_sql,
                    {
                        "collections": list(collections),
                        "query": query,
                        "snippet_chars": snippet_chars,
                        "top_k": top_k,
                    },
                )

                for row in cur:
                    yield SearchHit(
                        id=row["chunk_id"],
                        distance=-float(row["rank"]),
                        metadata=self._row_metadata(row),
                        snippet=row["snippet"] or "",
                        tags=tuple(row.get("tags") or ()),
                    )
        except Exception as e:
            raise KnowledgeBaseError(
                f"postgres fts search failed for collections "
                f"{list(collections)!r}: {type(e).__name__}: {e}",
            ) from e

    @staticmethod
    def _row_metadata(row: dict[str, Any]) -> dict[str, str]:
        # `metadata` хранится jsonb; psycopg возвращает dict. Системные
        # колонки добавляем поверх (без удаления tags) — проекция в llm.*
        # делается выше (search.schema), здесь отдаём сырой набор ключей.
        raw = row.get("metadata") or {}
        out: dict[str, str] = (
            {str(k): str(v) for k, v in raw.items() if v is not None}
            if isinstance(raw, dict)
            else {}
        )
        for key in ("source_id", "chunk_index", "content_hash"):
            value = row.get(key)
            if value is not None:
                out.setdefault(key, str(value))
        return out
