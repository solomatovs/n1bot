"""`FtsSearchConfig` + `PgFtsKnowledgeBase` — read-only FTS по whitelist-таблице.

В одном модуле:
- `FtsSearchConfig`     — корневой tool-конфиг (`[tool.kb.fts_search]`).
- `PgFtsKnowledgeBase`  — runtime-сервис. Принимает `cfg: FtsSearchConfig`,
                          открывает pool внутри через `open_kb_pool` (singleton
                          по DSN). `search(query, top_k)` собирает безопасный
                          SQL через `psycopg.sql.Identifier` (никакая часть
                          identifier-ов не приходит от LLM).

Таблица для FTS задаётся оператором через `FtsSearchConfig.index`
(`IndexSpec`). DSN может отличаться от KB-store: оператор может закрепить
read-only роль с ограниченным `GRANT SELECT` на одну whitelist-таблицу.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from psycopg.sql import Composable, Composed
from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.postgres_connection import PostgresConnection
from boba.tool.kb.core.postgres_pool import open_kb_pool
from boba.tool.kb.fts.models import FtsHit, IndexSpec

logger = logging.getLogger(__name__)

__all__ = ["FtsQueryError", "FtsSearchConfig", "PgFtsKnowledgeBase"]


class FtsSearchConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `fts_search` + сервиса `PgFtsKnowledgeBase`.

    Config-секция: `[tool.kb.fts_search]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.fts_search",
    )

    connection: PostgresConnection
    index: IndexSpec
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции `ts_headline`: MaxFragments,MaxWords,MinWords,"
            "StartSel,StopSel,..."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k для fts_search.",
    )


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
        *,
        cfg: FtsSearchConfig,
    ) -> None:
        self._cfg = cfg
        self._pool = open_kb_pool(cfg.connection)
        logger.info(
            "PgFtsKnowledgeBase opened: index=%s (%s.%s)",
            cfg.index.name,
            cfg.index.schema,
            cfg.index.table,
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
                f"fts query failed for index {self._cfg.index.name!r}: "
                f"{type(e).__name__}: {e}",
            ) from e

        return [self._row_to_hit(row, column_names) for row in rows]

    def _build_query(
        self,
        query: str,
        top_k: int,
    ) -> tuple[Composed, tuple[Any, ...]]:
        from psycopg import sql  # noqa: PLC0415

        spec = self._cfg.index
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
            self._cfg.snippet_options,
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
