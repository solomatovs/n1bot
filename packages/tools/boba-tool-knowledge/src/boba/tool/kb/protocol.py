"""Контракт kb-payload'а: эмбеддинг и SQL поиска идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.db.postgres import PostgresConfig
from boba.toolkit.secrets import SecretDump

__all__ = ["EmbeddingRef", "KbNode", "KbSearchMethod", "KbSearchRequest"]


class KbNode(StrEnum):
    """Узел реестра стадий kb; значение — оно же поле op запроса payload'а."""

    SEARCH = "kb_search"


class KbSearchMethod(StrEnum):
    """Канал поиска: по нему payload выбирает SQL-ветку, имя узла одно."""

    VECTOR = "vector"
    FTS = "fts"


class EmbeddingRef(BaseModel):
    """Эмбеддер внутри песочницы: имя модели и каталог весов."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, description="Имя модели fastembed.")
    cache_dir: str = Field(
        description="Каталог весов в песочнице; пусто — дефолт fastembed.",
    )


class KbSearchRequest(BaseModel):
    """Поиск по чанкам: SQL-шаблон приходит с хоста, данные — из БД."""

    model_config = ConfigDict(extra="forbid")

    op: KbNode
    method: KbSearchMethod
    connection: PostgresConfig = Field(
        description=(
            "Профиль подключения целиком: payload сам получает по нему TGT из keytab."
        ),
    )
    schema_name: str = Field(min_length=1)
    chunks_table: str = Field(min_length=1)
    collections: Sequence[str] = Field(min_length=1)
    query: str = Field(min_length=1)
    top_k: int = Field(ge=1)
    snippet_chars: int = Field(ge=1)
    embedding: EmbeddingRef

    @field_serializer("connection", when_used="json")
    def _dump_connection(self, value: PostgresConfig) -> dict[str, Any]:
        """tool_args песочницы — доверенный канал: только здесь пароль раскрыт."""
        return SecretDump.of(value)
