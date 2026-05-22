"""PgFtsKnowledgeBase: read-only обёртка над PostgresPool + ОДИН whitelist-индекс.

Таблица для FTS задаётся оператором через `FtsConfig.index` (IndexSpec).
`search(query, top_k)` собирает безопасный SQL через `psycopg.sql.Identifier`
(никакая часть identifier-ов не приходит от LLM).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from psycopg.sql import Composable, Composed

from boba.db.postgres import PostgresPool
from boba.tool.kb.fts.models import FtsHit, IndexSpec

logger = logging.getLogger(__name__)

__all__ = ["FtsQueryError", "PgFtsKnowledgeBase"]


class FtsQueryError(RuntimeError):
    """Ошибка выполнения FTS-запроса (psycopg-side)."""


class PgFtsKnowledgeBase:
    """FTS-поиск по одной whitelist-таблице (`IndexSpec`).

    Identifier'ы (schema/table/column) приходят из конфига (TOML), не от
    LLM, и подставляются через `psycopg.sql`. Параметры запроса (`query`,
    `top_k`) — всегда через placeholder'ы, без склейки.
    """

    def __init__(
        self,
        pool: PostgresPool,
        index: IndexSpec,
        snippet_options: str,
    ) -> None:
        self._pool = pool
        self._index = index
        self._snippet_options = snippet_options
        logger.info(
            "PgFtsKnowledgeBase opened: index=%s (%s.%s)",
            index.name,
            index.schema,
            index.table,
        )

    def search(self, query: str, top_k: int) -> list[FtsHit]:
        stmt, params = self._build_query(query, top_k)
        try:
            with self._pool.cursor() as cur:
                cur.execute(stmt, params)
                rows = cur.fetchall()
                column_names = [d.name for d in (cur.description or [])]
        except Exception as e:
            raise FtsQueryError(
                f"fts query failed for index {self._index.name!r}: "
                f"{type(e).__name__}: {e}",
            ) from e

        return [self._row_to_hit(row, column_names) for row in rows]

    def _build_query(
        self,
        query: str,
        top_k: int,
    ) -> tuple[Composed, tuple[Any, ...]]:
        from psycopg import sql  # noqa: PLC0415

        spec = self._index
        meta_select: Composable = sql.SQL("")
        if spec.metadata_columns:
            meta_select = sql.SQL(", ") + sql.SQL(", ").join(
                sql.Identifier(c) for c in spec.metadata_columns
            )

        stmt = sql.SQL(
            "SELECT "
            "{id_col}::text AS _id, "
            "ts_rank_cd({tsv_col}, q) AS _score, "
            "ts_headline(%s::regconfig, {snippet_col}, q, %s) AS _snippet"
            "{meta_select} "
            "FROM {table}, "
            "websearch_to_tsquery(%s::regconfig, %s) q "
            "WHERE {tsv_col} @@ q "
            "ORDER BY _score DESC "
            "LIMIT %s"
        ).format(
            id_col=sql.Identifier(spec.id_column),
            tsv_col=sql.Identifier(spec.tsv_column),
            snippet_col=sql.Identifier(spec.snippet_column),
            table=sql.Identifier(spec.schema, spec.table),
            meta_select=meta_select,
        )
        params: tuple[Any, ...] = (
            spec.language,
            self._snippet_options,
            spec.language,
            query,
            top_k,
        )
        return stmt, params

    @staticmethod
    def _row_to_hit(
        row: Sequence[Any],
        column_names: Sequence[str],
    ) -> FtsHit:
        # _id, _score, _snippet — first three columns; остальные — metadata.
        hit_id = "" if row[0] is None else str(row[0])
        score = float(row[1]) if row[1] is not None else 0.0
        snippet = "" if row[2] is None else str(row[2])
        metadata: dict[str, str] = {}
        for i in range(3, len(row)):
            value = row[i]
            if value is None:
                continue
            metadata[column_names[i]] = str(value)
        return FtsHit(
            id=hit_id,
            score=score,
            metadata=metadata,
            snippet=snippet,
        )
