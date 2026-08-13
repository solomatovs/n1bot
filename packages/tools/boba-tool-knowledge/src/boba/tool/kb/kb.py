"""Read-only KB над postgres+pgvector: поиск по чанкам для kb_search."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import Field

from boba.db.pgvector import PostgresStoreConfig
from boba.tool.kb.caller import KbCaller
from boba.tool.kb.embedding import EmbeddingModel
from boba.tool.kb.models import KnowledgeBaseError, SearchHit
from boba.tool.kb.protocol import KbSearchMethod
from boba.toolkit.launcher import LauncherError, RowCollector

logger = logging.getLogger(__name__)

__all__ = [
    "PostgresKnowledgeBase",
    "PostgresKnowledgeBaseConfig",
    "RowKey",
]


class RowKey(StrEnum):
    """Колонки строки выдачи, которыми дополняется metadata чанка."""

    SOURCE_ID = "source_id"
    CHUNK_INDEX = "chunk_index"
    CONTENT_HASH = "content_hash"


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

    def search(
        self,
        *,
        method: KbSearchMethod,
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
                method=method,
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
            yield self._hit(row, method=method)

    @classmethod
    def _hit(cls, row: dict[str, Any], *, method: KbSearchMethod) -> SearchHit:
        """Ранг FTS растёт с релевантностью — знак приводит его к дистанции."""
        if method is KbSearchMethod.VECTOR:
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

    @staticmethod
    def _row_metadata(row: dict[str, Any]) -> dict[str, str]:
        raw: object = row.get("metadata")

        out: dict[str, str] = {}
        if isinstance(raw, dict):
            fields: dict[Any, Any] = raw
            for key, value in fields.items():
                if value is None:
                    continue

                out[str(key)] = str(value)

        for key in RowKey:
            value = row.get(key.value)
            if value is None:
                continue

            out.setdefault(key.value, str(value))

        return out
