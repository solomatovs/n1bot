"""Конфиг read-side KB над postgres+pgvector; исполнение живёт в kb.tools."""

from __future__ import annotations

from pydantic import Field

from boba.db.pgvector import PostgresStoreConfig
from boba.llm.embedding import EmbeddingConfig

__all__ = [
    "PostgresKnowledgeBaseConfig",
]


class PostgresKnowledgeBaseConfig(PostgresStoreConfig):
    """Composite-конфиг read-side KB: языки FTS зашиты и в SQL-шаблоны, и в DDL
    tsv-колонки (migrations/002_multilang_tsv.sql) — оба места должны быть синхронны."""

    embedding: EmbeddingConfig
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма потока выдачи (символов).",
    )
