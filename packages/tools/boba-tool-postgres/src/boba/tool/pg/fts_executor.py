"""FTS-executor для tool fts_search."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from psycopg.sql import Composable, Composed
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.db.postgres import PostgresConnection, PostgresPool

logger = logging.getLogger(__name__)

__all__ = [
    "FtsExecutorConfig",
    "FtsHit",
    "FtsQueryError",
    "IndexSpec",
    "PgFtsExecutor",
]


class IndexSpec(BaseModel):
    """Декларация одной FTS-таблицы оператора."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        protected_namespaces=(),
    )

    schema: str = Field(default="public", description="PG schema таблицы.")
    table: str = Field(description="Имя таблицы (без schema).")
    id_column: str = Field(description="Колонка-PK; её значение возвращается в hit.id.")
    tsv_column: str = Field(description="Колонка типа tsvector")
    snippet_column: str = Field(
        description="Текстовая колонка для ts_headline (обычно body/content).",
    )
    language: str = Field(
        default="english",
        description="PG search config (regconfig): russian/english/simple/...",
    )
    metadata_columns: list[str] = Field(
        default_factory=list,
        description=(
            "Колонки, отдаваемые как hit.metadata (например title, source_url)."
        ),
    )


@dataclass(frozen=True)
class FtsHit:
    """Один результат fts_search; score — `ts_rank_cd` (больше = релевантнее)."""

    id: str
    score: float
    metadata: Mapping[str, str]
    snippet: str


class FtsExecutorConfig(BaseModel):
    """Конфиг для PgFtsExecutor."""

    databases: dict[str, PostgresConnection] = Field(
        description="Shared whitelist подключений — тот же, что у SQL-tool'ов.",
    )
    whitelists: dict[str, IndexSpec] = Field(
        description="FTS-IndexSpec на каждый target. Ключи подмножество databases.",
    )
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции ts_headline: MaxFragments,MaxWords,MinWords,StartSel,StopSel,..."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.databases:
            msg = "tool.pg.fts_search: пустой databases"
            raise ValueError(msg)
        if not self.whitelists:
            msg = "tool.pg.fts_search.whitelists: пусто"
            raise ValueError(msg)
        stray = sorted(set(self.whitelists) - set(self.databases))
        if stray:
            msg = (
                f"tool.pg.fts_search.whitelists: target(ы) {stray!r} нет "
                f"в shared databases ({sorted(self.databases)!r})"
            )
            raise ValueError(msg)
        return self

    def resolve(self, target: str) -> tuple[PostgresConnection, IndexSpec]:
        """Вернуть (connection, IndexSpec); ValueError если target не задан."""
        conn = self.databases.get(target)
        spec = self.whitelists.get(target)
        if conn is None or spec is None:
            searchable = sorted(set(self.databases) & set(self.whitelists))
            msg = (
                f"fts_search: target {target!r} нет в whitelist "
                f"(searchable={searchable})"
            )
            raise ValueError(msg)
        return conn, spec

    @staticmethod
    def session_options(conn: PostgresConnection) -> dict[str, str]:
        """Session-level GUC, зашиваемые в options DSN."""
        return {
            "default_transaction_read_only": "on",
            "statement_timeout": str(conn.statement_timeout_ms),
        }


class FtsQueryError(RuntimeError):
    """Ошибка выполнения FTS-запроса."""


class PgFtsExecutor:
    """FTS-поиск по профилю из whitelist."""

    def __init__(
        self,
        *,
        cfg: FtsExecutorConfig,
    ) -> None:
        self._cfg = cfg
        logger.info(
            "PgFtsExecutor opened: targets=%s",
            sorted(cfg.databases),
        )

    def allowed_targets(self) -> list[str]:
        """Searchable targets: пересечение databases и whitelists."""
        return sorted(set(self._cfg.databases) & set(self._cfg.whitelists))

    def search(self, query: str, *, target: str, top_k: int) -> list[FtsHit]:
        conn, spec = self._cfg.resolve(target)
        pool = PostgresPool.get(
            conn.to_pool_config(
                session_options=self._cfg.session_options(conn),
            ),
        )

        stmt, params = self._build_query(spec, query, top_k)
        try:
            with pool.cursor() as cur:
                cur.execute(stmt, params)
                rows = cur.fetchall()
                column_names = [d.name for d in (cur.description or [])]
        except Exception as e:
            raise FtsQueryError(
                f"fts query failed for target={target!r} "
                f"whitelist={spec.schema}.{spec.table}: "
                f"{type(e).__name__}: {e}",
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
