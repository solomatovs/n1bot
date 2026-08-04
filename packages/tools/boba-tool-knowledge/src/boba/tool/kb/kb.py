"""Read-only KB над postgres+pgvector: vector_search и fts_search для search-tools."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from boba.db.postgres import PostgresConfig
from boba.tool.kb.caller import KbCaller
from boba.tool.kb.embedding import EmbeddingModel
from boba.tool.kb.models import KnowledgeBaseError, SearchHit
from boba.tool.kb.postgres import PostgresStoreSchema
from boba.toolkit.launcher import LauncherError

logger = logging.getLogger(__name__)

__all__ = [
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
]


class PostgresKnowledgeBaseConfig(BaseModel):
    """Composite-конфиг read-side KB: языки FTS зашиты и в SQL-шаблоны, и в DDL
    tsv-колонки (migrations/002_multilang_tsv.sql) — оба места должны быть синхронны."""

    connection: PostgresConfig
    tables: PostgresStoreSchema
    embedding: EmbeddingModel


class PostgresKnowledgeBase:
    """Доступ к хранилищу kb."""

    def __init__(
        self,
        *,
        cfg: PostgresKnowledgeBaseConfig,
        caller: KbCaller,
    ) -> None:
        self._cfg = cfg
        self._caller = caller
        logger.info(
            "PostgresKnowledgeBase opened chunks=%s.%s",
            cfg.tables.pg_schema,
            cfg.tables.chunks_table,
        )

    def _search(  # noqa: PLR0913
        self,
        *,
        op: str,
        collections: list[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        sql_template: str,
    ) -> Iterable[SearchHit]:
        """Эмбеддинг и SQL исполняет payload; здесь только разбор строк."""
        try:
            answer = self._caller.search(
                op=op,
                connection=self._cfg.connection,
                sql_template=sql_template,
                schema_name=self._cfg.tables.pg_schema,
                chunks_table=self._cfg.tables.chunks_table,
                collections=collections,
                query=query,
                top_k=top_k,
                snippet_chars=snippet_chars,
                embedding={
                    "model": self._cfg.embedding.model,
                    "cache_dir": self._cfg.embedding.cache_dir,
                },
            )
        except LauncherError as e:
            raise KnowledgeBaseError(
                f"kb search failed for collections {collections!r}: {e}",
            ) from e
        for row in answer.rows:
            yield self._hit(row, op=op)

    @classmethod
    def _hit(cls, row: dict[str, Any], *, op: str) -> SearchHit:
        if op == KbCaller.VECTOR_OP:
            distance = float(row["distance"])
        else:
            distance = -float(row["rank"])
        return SearchHit(
            id=row["chunk_id"],
            distance=distance,
            metadata=cls._row_metadata(row),
            snippet=row["snippet"] or "",
            tags=tuple(row.get("tags") or ()),
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
        return self._search(
            op=KbCaller.VECTOR_OP,
            collections=collections,
            query=query,
            top_k=top_k,
            snippet_chars=snippet_chars,
            sql_template=sql_template,
        )

    def fts_search(
        self,
        *,
        collections: list[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        sql_template: str,
    ) -> Iterable[SearchHit]:
        return self._search(
            op=KbCaller.FTS_OP,
            collections=collections,
            query=query,
            top_k=top_k,
            snippet_chars=snippet_chars,
            sql_template=sql_template,
        )

    @staticmethod
    def _row_metadata(row: dict[str, Any]) -> dict[str, str]:
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
