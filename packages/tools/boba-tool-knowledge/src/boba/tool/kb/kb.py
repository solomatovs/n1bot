"""Конфиги плагина kb, секция [tool.kb]: старый поиск по чанкам kb_chunks (период
миграции) и поиск по схеме ix. Обе модели читают одну секцию, каждая свои поля;
исполнение живёт в kb.chunks и kb.tools."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from boba.db.pgvector.config import PostgresStoreConfig
from boba.db.postgres.profile import PostgresConfig
from boba.llm.embedding import EmbeddingConfig
from boba.toolkit.types import SecretRevealing

__all__ = [
    "KbChunksConfig",
    "KbToolConfig",
    "PostgresKnowledgeBaseConfig",
]


class PostgresKnowledgeBaseConfig(PostgresStoreConfig):
    """Composite-конфиг read-side KB по kb_chunks: языки FTS зашиты и в SQL-шаблоны,
    и в DDL tsv-колонки (migrations/002_multilang_tsv.sql) — оба места должны быть
    синхронны."""

    embedding: EmbeddingConfig
    max_result_chars: int = Field(
        default=1_000_000,
        ge=1,
        description="Потолок суммарного объёма потока выдачи (символов).",
    )


class KbChunksConfig(SecretRevealing, PostgresKnowledgeBaseConfig):
    """Секция [tool.kb] глазами старого поиска: подключение, таблицы kb_chunks,
    коллекция и эмбеддер."""

    SECTION: ClassVar[str] = "tool.kb"

    collection: str = Field(
        min_length=1,
        description="Имя коллекции чанков, по которой идёт поиск.",
    )


class KbToolConfig(SecretRevealing):
    """Секция [tool.kb] глазами поиска по ix: профиль postgres базы ix, имя схемы,
    эмбеддер запроса той же модели, что у таблицы векторов, и потолок символов
    одного ответа."""

    SECTION: ClassVar[str] = "tool.kb"

    connection: PostgresConfig
    db_schema: str = Field(min_length=1)
    embedding: EmbeddingConfig
    max_result_chars: int = Field(ge=1)
