"""`PgFtsExecutor` — read-only FTS-сервис по одной whitelist-таблице.

Tool `fts_search` принимает `FtsExecutorConfig` (composite-BaseModel:
connection + whitelist + snippet_options) как nested-поле в своём
tool-конфиге и строит `PgFtsExecutor` inline.

Безопасность аналогична остальным `boba-tool-postgres`-тулзам:
- DSN-роль должна быть read-only (`GRANT SELECT ON ...`). PG отклоняет
  любые INSERT/UPDATE/DDL с `permission denied`.
- `default_transaction_read_only=on` (libpq `options=-c ...`) — двойная
  защита поверх прав роли.
- `statement_timeout=<ms>` (тоже через libpq `options=`) — runtime-cap.
- Identifier'ы (schema/table/column) приходят из конфига (TOML), не от
  LLM, и подставляются через `psycopg.sql.Identifier`. Параметры запроса
  (`query`, `top_k`) — всегда через placeholder'ы, без склейки.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.sql import Composable, Composed
from pydantic import BaseModel, ConfigDict, Field

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
    """Декларация одной FTS-таблицы оператора (`FtsExecutorConfig.whitelist`).

    Имена колонок/таблицы приходят из конфига (TOML), не от LLM, и
    подставляются в SQL через `psycopg.sql.Identifier`. Параметры запроса
    (`query`, `top_k`) — всегда через placeholder'ы, без склейки.
    """

    # protected_namespaces=() — гасим предупреждение про field "schema",
    # которое shadow'ит deprecated v1-метод BaseModel.schema(); сам метод
    # в pydantic v2 заменён на model_json_schema().
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        protected_namespaces=(),
    )

    schema: str = Field(default="public", description="PG schema таблицы.")
    table: str = Field(description="Имя таблицы (без schema).")
    id_column: str = Field(description="Колонка-PK; её значение возвращается в hit.id.")
    tsv_column: str = Field(
        description="Колонка типа tsvector (обычно GENERATED + GIN-индекс).",
    )
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
    """Composite-конфиг для `PgFtsExecutor`: connection + whitelist + опции.

    Не settings (нет `config_path`) — встраивается как nested-поле в
    tool-конфиг `FtsSearchConfig`. Благодаря рекурсивному flatten'у поля
    whitelist'а (schema/table/columns/language) и snippet_options лежат
    плоско в корневой TOML-секции tool'а.
    """

    connection: PostgresConnection
    whitelist: IndexSpec
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции `ts_headline`: MaxFragments,MaxWords,MinWords,"
            "StartSel,StopSel,..."
        ),
    )

    def session_options(self) -> dict[str, str]:
        """Session-level GUC, зашиваемые в `options=`-параметр DSN."""
        return {
            "default_transaction_read_only": "on",
            "statement_timeout": str(self.connection.statement_timeout_ms),
        }


class FtsQueryError(RuntimeError):
    """Ошибка выполнения FTS-запроса (psycopg-side)."""


class PgFtsExecutor:
    """FTS-поиск по одной whitelist-таблице (`IndexSpec`)."""

    def __init__(
        self,
        *,
        cfg: FtsExecutorConfig,
    ) -> None:
        self._cfg = cfg
        self._pool = PostgresPool.get(
            cfg.connection.to_pool_config(
                session_options=cfg.session_options(),
            ),
        )
        logger.info(
            "PgFtsExecutor opened: whitelist=%s.%s",
            cfg.whitelist.schema,
            cfg.whitelist.table,
        )

    def search(self, query: str, top_k: int) -> list[FtsHit]:
        stmt, params = self._build_query(query, top_k)
        try:
            with self._pool.cursor() as cur:
                cur.execute(stmt, params)
                rows = cur.fetchall()
                column_names = [d.name for d in (cur.description or [])]
        except Exception as e:
            spec = self._cfg.whitelist
            raise FtsQueryError(
                f"fts query failed for whitelist {spec.schema}.{spec.table}: "
                f"{type(e).__name__}: {e}",
            ) from e

        return [self._row_to_hit(row, column_names) for row in rows]

    def _build_query(
        self,
        query: str,
        top_k: int,
    ) -> tuple[Composed, tuple[Any, ...]]:
        from psycopg import sql  # noqa: PLC0415

        spec = self._cfg.whitelist
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
