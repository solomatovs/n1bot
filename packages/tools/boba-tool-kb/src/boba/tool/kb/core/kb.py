"""Read-only KB-обёртка над postgres+pgvector — для search-tools'ов.

`PostgresKnowledgeBase` — тонкий wrapper над connection pool + embedder,
с двумя операциями tool-уровня:

- `search(...)`         — **гибридный** retrieval: top-K от pgvector +
                          top-K от FTS, склейка через Reciprocal Rank
                          Fusion. Используется `kb_search`-tool'ом.
                          RRF-параметры — `KbConfig.rrf_k`/`rrf_pool`.
- `vector_search(...)`  — **чистый** semantic top-K от pgvector (cosine
                          via `<=>`). Используется `vector_search`-tool'ом.
                          Полезен, когда FTS-канал шумит/мешает (короткие
                          запросы, эмбеддинг лучше ловит синонимы).

Snippet обрезан по `snippet_chars`, метадата нормализована под формат
tool'ов (тот же shape, что и у `search`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, ClassVar

from psycopg import sql
from pydantic import BaseModel, Field

from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.models import SearchHit
from boba.tool.kb.core.postgres_connection import PostgresConnection
from boba.tool.kb.core.postgres_pool import open_kb_pool
from boba.tool.kb.core.postgres_store_schema import PostgresStoreSchema

logger = logging.getLogger(__name__)

__all__ = [
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
]


class PostgresKnowledgeBaseConfig(BaseModel):
    """Composite-конфиг для read-side KB.

    Поля: connection + tables + embedding + RRF/FTS-params.
    """

    connection: PostgresConnection
    tables: PostgresStoreSchema
    embedding: EmbeddingModel
    snippet_chars: int = Field(
        default=300,
        ge=1,
        description="Максимальная длина сниппета документа в search-результатах.",
    )
    fts_language: str = Field(
        default="russian",
        description=(
            "PostgreSQL text search configuration для tsvector колонки. "
            "Должен быть установленным `pg_ts_config` именем (`russian`, "
            "`english`, `simple`, ...). Меняется только пересозданием "
            "kb_chunks (см. `migrations/001_init.sql`)."
        ),
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        description=(
            "Константа RRF (Reciprocal Rank Fusion). Стандарт литературы — "
            "60. Больше → плавнее склейка, меньше → агрессивнее доминирует "
            "первый rank-1 из любого канала."
        ),
    )
    rrf_pool: int = Field(
        default=40,
        ge=1,
        description=(
            "Сколько top-K брать из каждого канала (vector + FTS) перед "
            "склейкой через RRF. Обычно 2-4x от итогового top_k."
        ),
    )


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
        self._pool = open_kb_pool(cfg.connection)
        self._embedder = build_embedder(cfg.embedding)
        logger.info(
            "PostgresKnowledgeBase opened dim=%d fts=%s rrf_k=%d pool=%d chunks=%s.%s",
            self._embedder.dim(),
            cfg.fts_language,
            cfg.rrf_k,
            cfg.rrf_pool,
            cfg.tables.schema,
            cfg.tables.chunks_table,
        )

    def search(
        self,
        *,
        collections: list[str],
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        """Гибридный hybrid retrieval с Reciprocal Rank Fusion.

        Ищет по объединению переданных коллекций: SQL-фильтр
        `collection = ANY(%(collections)s)`. Пустой список — ошибка
        (валидируется в `SearchConfig`).

        Алгоритм (один SQL, два CTE):
          1. vec CTE: top-`rrf_pool` по cosine-distance (`<=>`).
          2. fts CTE: top-`rrf_pool` по `ts_rank` для `plainto_tsquery`.
          3. Outer SELECT: full-outer-join по chunk_id, RRF-скор =
             `1/(k + vec_rk) + 1/(k + fts_rk)` (отсутствующий ранг →
             вклад нулевой), сортируем по RRF DESC, берём `top_k`.

        `distance` в результате — **отрицательный RRF-скор**: семантика
        «меньше = ближе/релевантнее», как у chromadb-distance. Чистый
        cosine-distance остаётся доступен через `PostgresChunkStore`.
        """
        embedding = list(self._embedder.embed_query(query))

        query_sql = self._SEARCH_SQL.format(
            dim=sql.Literal(self._embedder.dim()),
            chunks_table=self._cfg.tables.chunks_ident(),
            schema=self._cfg.tables.schema_ident(),
        )
        try:
            with self._pool.dict_cursor() as cur:
                cur.execute(
                    query_sql,
                    {
                        "collections": list(collections),
                        "embedding": embedding,
                        "query": query,
                        "lang": self._cfg.fts_language,
                        "rrf_k": self._cfg.rrf_k,
                        "rrf_pool": self._cfg.rrf_pool,
                        "snippet_chars": self._cfg.snippet_chars,
                        "top_k": top_k,
                    },
                )
                rows = cur.fetchall()
        except Exception as e:
            raise KnowledgeBaseError(
                f"postgres hybrid search failed for collections "
                f"{list(collections)!r}: {type(e).__name__}: {e}",
            ) from e

        return [
            SearchHit(
                id=row["chunk_id"],
                distance=-float(row["rrf"]),
                metadata=self._row_metadata(row),
                snippet=row["snippet"] or "",
            )
            for row in rows
        ]

    def vector_search(
        self,
        *,
        collections: list[str],
        query: str,
        top_k: int,
    ) -> Iterable[SearchHit]:
        """Чистый vector top-K (cosine via `<=>`) без FTS-канала.

        Ищет по объединению переданных коллекций: SQL-фильтр
        `collection = ANY(%(collections)s)`.

        `distance` — cosine-distance pgvector'а (меньше = ближе, диапазон
        [0..2]). Семантика consistent с `search`'ем (тоже меньше = ближе),
        но физический смысл другой (cosine, не отрицательный RRF).

        Snippet режется до `snippet_chars` (как и в `search`), метадата
        нормализуется тем же `_row_metadata`.
        """
        embedding = list(self._embedder.embed_query(query))
        query_sql = self._VECTOR_SEARCH_SQL.format(
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
                        "snippet_chars": self._cfg.snippet_chars,
                        "top_k": top_k,
                    },
                )

                for row in cur.fetchall():
                    yield SearchHit(
                        id=row["chunk_id"],
                        distance=float(row["distance"]),
                        metadata=self._row_metadata(row),
                        snippet=row["snippet"] or "",
                    )
        except Exception as e:
            raise KnowledgeBaseError(
                f"postgres vector search failed for collections "
                f"{list(collections)!r}: {type(e).__name__}: {e}",
            ) from e

    @staticmethod
    def _row_metadata(row: dict[str, Any]) -> dict[str, str]:
        # `metadata` хранится jsonb; psycopg возвращает dict. Системные
        # колонки добавляем поверх (без удаления tags) — UI/citation
        # ожидают source_url/anchor в metadata-mapping'е.
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

    # SQL для поиска векторов совместно с полнотекстовым поисков
    # возвращает единный набор. WHERE collection = ANY(%(collections)s) —
    # psycopg сериализует list[str] в text[].
    _SEARCH_SQL: ClassVar[sql.SQL] = sql.SQL("""
    WITH vec AS (
        SELECT chunk_id,
               row_number() OVER (
                   ORDER BY (embedding::vector({dim})) <=> %(embedding)s::vector
               ) AS rk
        FROM {chunks_table}
        WHERE collection = ANY(%(collections)s) AND embedding IS NOT NULL
        ORDER BY (embedding::vector({dim})) <=> %(embedding)s::vector
        LIMIT %(rrf_pool)s
    ),
    fts AS (
        SELECT chunk_id,
               row_number() OVER (
                   ORDER BY ts_rank_cd(tsv, q) DESC
               ) AS rk
        FROM {chunks_table},
             plainto_tsquery(%(lang)s::regconfig,
             {schema}.immutable_unaccent(%(query)s)) q
        WHERE collection = ANY(%(collections)s) AND tsv @@ q
        ORDER BY ts_rank_cd(tsv, q) DESC
        LIMIT %(rrf_pool)s
    ),
    fused AS (
        SELECT
            COALESCE(v.chunk_id, f.chunk_id) AS chunk_id,
            (CASE WHEN v.rk IS NULL THEN 0.0
                  ELSE 1.0 / (%(rrf_k)s + v.rk) END)
            + (CASE WHEN f.rk IS NULL THEN 0.0
                    ELSE 1.0 / (%(rrf_k)s + f.rk) END) AS rrf
        FROM vec v
        FULL OUTER JOIN fts f USING (chunk_id)
    )
    SELECT c.chunk_id,
           c.source_id,
           c.chunk_index,
           c.content_hash,
           c.metadata,
           c.tags,
           LEFT(c.format_content, %(snippet_chars)s) AS snippet,
           fused.rrf AS rrf
    FROM fused
    JOIN {chunks_table} c USING (chunk_id)
    WHERE c.collection = ANY(%(collections)s)
    ORDER BY fused.rrf DESC
    LIMIT %(top_k)s
    """)

    # SQL для поиска только в векторной таблицу
    _VECTOR_SEARCH_SQL: ClassVar[sql.SQL] = sql.SQL("""
    SELECT c.chunk_id,
           c.source_id,
           c.chunk_index,
           c.content_hash,
           c.metadata,
           c.tags,
           LEFT(c.format_content, %(snippet_chars)s) AS snippet,
           (c.embedding::vector({dim})) <=> %(embedding)s::vector AS distance
    FROM {chunks_table} c
    WHERE c.collection = ANY(%(collections)s) AND c.embedding IS NOT NULL
    ORDER BY distance ASC
    LIMIT %(top_k)s
    """)
