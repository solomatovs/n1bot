"""Read-only KB над postgres+pgvector: vector_search и fts_search для search-tools."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from pydantic import Field

from boba.db.pgvector import PostgresStoreConfig
from boba.tool.kb.caller import KbCaller
from boba.tool.kb.embedding import EmbeddingModel
from boba.tool.kb.models import KnowledgeBaseError, SearchHit
from boba.tool.kb.protocol import KbNode
from boba.toolkit.launcher import LauncherError, RowCollector

logger = logging.getLogger(__name__)

__all__ = [
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
]


class PostgresKnowledgeBaseConfig(PostgresStoreConfig):
    """Composite-конфиг read-side KB: соединение, таблицы, эмбеддер и потолок выдачи."""

    embedding: EmbeddingModel
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма потока выдачи (символов).",
    )


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

    def _search(
        self,
        *,
        node: KbNode,
        collections: list[str],
        query: str,
        top_k: int,
        snippet_chars: int,
    ) -> Iterable[SearchHit]:
        """Эмбеддинг и SQL исполняет payload; здесь только разбор строк."""
        collector = RowCollector(
            max_chars=self._cfg.max_result_chars,
            limit_rows=top_k,
        )

        try:
            self._caller.search(
                node=node,
                collections=collections,
                query=query,
                top_k=top_k,
                snippet_chars=snippet_chars,
                sink=collector,
            )
        except LauncherError as e:
            raise KnowledgeBaseError(
                f"kb search failed for collections {collections!r}: {e}",
            ) from e

        for row in collector.rows():
            yield self._hit(row, node=node)

    @classmethod
    def _hit(cls, row: dict[str, Any], *, node: KbNode) -> SearchHit:
        if node is KbNode.VECTOR:
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
    ) -> Iterable[SearchHit]:
        return self._search(
            node=KbNode.VECTOR,
            collections=collections,
            query=query,
            top_k=top_k,
            snippet_chars=snippet_chars,
        )

    def fts_search(
        self,
        *,
        collections: list[str],
        query: str,
        top_k: int,
        snippet_chars: int,
    ) -> Iterable[SearchHit]:
        return self._search(
            node=KbNode.FTS,
            collections=collections,
            query=query,
            top_k=top_k,
            snippet_chars=snippet_chars,
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
