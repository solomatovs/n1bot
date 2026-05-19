"""PgFtsKnowledgeBase: read-only обёртка над PostgresPool + whitelist FTS-индексов."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from psycopg.sql import Composable, Composed

from boba.db.postgres import PostgresPool
from boba.tool.postgres_fts.models import FtsHit, IndexInfo, IndexSpec

logger = logging.getLogger(__name__)

__all__ = ["FtsQueryError", "IndexNotFoundError", "PgFtsKnowledgeBase"]


class IndexNotFoundError(KeyError):
    """Индекс не зарегистрирован в whitelist'е плагина."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class FtsQueryError(RuntimeError):
    """Ошибка выполнения FTS-запроса (psycopg-side)."""


class PgFtsKnowledgeBase:
    """Whitelist FTS-индексов поверх PostgresPool.

    `list_indexes()` — статичный список из конфига; не ходит в БД.
    `search(...)` — выполняет один `websearch_to_tsquery`-запрос
    к конкретному индексу. Identifier'ы (schema/table/column) собраны
    из конфига (TOML), не от LLM, и подставляются через `psycopg.sql`.
    """

    def __init__(
        self,
        pool: PostgresPool,
        indexes: Sequence[IndexSpec],
        snippet_options: str,
    ) -> None:
        self._pool = pool
        self._indexes: dict[str, IndexSpec] = {idx.name: idx for idx in indexes}
        self._snippet_options = snippet_options
        logger.info(
            "PgFtsKnowledgeBase opened: %d index(es) [%s]",
            len(self._indexes),
            ", ".join(sorted(self._indexes)),
        )

    def list_indexes(self) -> list[IndexInfo]:
        return [
            IndexInfo(name=idx.name, description=idx.description)
            for idx in self._indexes.values()
        ]

    def search(
        self,
        index: str,
        query: str,
        top_k: int,
    ) -> list[FtsHit]:
        spec = self._indexes.get(index)
        if spec is None:
            raise IndexNotFoundError(index)

        stmt, params = self._build_query(spec, query, top_k)
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.execute(stmt, params)
                rows = cur.fetchall()
                column_names = [d.name for d in (cur.description or [])]
        except Exception as e:
            raise FtsQueryError(
                f"fts query failed for index {index!r}: {type(e).__name__}: {e}",
            ) from e

        return [self._row_to_hit(row, column_names) for row in rows]

    def _build_query(
        self,
        spec: IndexSpec,
        query: str,
        top_k: int,
    ) -> tuple[Composed, tuple[Any, ...]]:
        from psycopg import sql  # noqa: PLC0415

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
