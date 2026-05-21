"""`PostgresPluginConfig` — конфиг секции `[tool.postgres]`.

Плагин-уровневое включение/allowlist (`enable`, `tools`) — забота
framework'а (`AgentBuilder.discover_plugins`); конфиг плагина их не
объявляет (`extra="ignore"`).
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["PostgresPluginConfig"]


class PostgresPluginConfig(BobaFlatSettings):
    """Postgres + pgvector KB tools: kb_search + kb_list_collections + kb_ingest."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.postgres",
    )

    dsn: str = Field(
        default="",
        description=(
            "PostgreSQL DSN. Поддерживается `postgres://user:pass@host:port/db` "
            "или libpq key-value. Обязателен."
        ),
    )
    pool_min_size: int = Field(
        default=1,
        ge=0,
        description="Минимальный размер connection pool'а (psycopg_pool).",
    )
    pool_max_size: int = Field(
        default=8,
        ge=1,
        description="Максимальный размер connection pool'а.",
    )
    fts_language: str = Field(
        default="russian",
        description=(
            "PostgreSQL text search configuration для tsvector колонки. "
            "Должен быть установленным `pg_ts_config` именем (`russian`, "
            "`english`, `simple`, ...). Меняется только пересозданием "
            "kb_chunks (см. migrations/001_init.sql)."
        ),
    )
    embedding_model: str = Field(
        default="",
        description=(
            "Имя embedding-модели для OpenAI-совместимого endpoint'а "
            "(LiteLLM / OpenAI / vLLM / Ollama). Пустая строка — ingest и "
            "kb_search будут падать fail-fast. Размерность вектора "
            "определяется автоматически по первому ответу модели "
            "(см. `OpenAICompatEmbedder.dim()`)."
        ),
    )
    embedding_base_url: str = Field(
        default="",
        description="OpenAI-совместимый endpoint embeddings.",
    )
    embedding_api_key: str = Field(
        default="",
        description="API key embeddings endpoint'а.",
    )
    snippet_chars: int = Field(
        default=300,
        ge=1,
        description="Максимальная длина сниппета документа в kb_search.",
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k.",
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        description=(
            "Константа RRF (Reciprocal Rank Fusion). Стандарт литературы — "
            "60. Больше → плавнее склейка, меньше → агрессивнее доминирует "
            "первый rank-1 из любого канала."
        ),
    )
    rrf_pool: int = Field(
        default=40,
        ge=1,
        description=(
            "Сколько top-K брать из каждого канала (vector + FTS) перед "
            "склейкой через RRF. Обычно 2-4x от итогового top_k."
        ),
    )
    chunk_size: int = Field(
        default=4000,
        ge=1,
        description=(
            "Целевой размер `format_content` чанка в символах (передаётся "
            "в `OverlapCharSplitter.chunk_size`). `StructuralChunker` "
            "уменьшает effective-budget на длину `prefix + repeat_header + "
            "repeat_footer`, чтобы итоговый чанк влез в лимит."
        ),
    )
    chunk_overlap: int = Field(
        default=0,
        ge=0,
        description=(
            "Перекрытие между соседними чанками в символах (передаётся в "
            "`OverlapCharSplitter.chunk_overlap`). 0 = без перекрытия "
            "(дефолт). Полезно для длинных контекстов, где разрыв "
            "посередине абзаца ухудшает retrieval."
        ),
    )
    ingest_folder: str = Field(
        default="",
        description=(
            "Папка с .md чанками для индексации. Оператор закрепляет "
            "выбор папки за собой — LLM не выбирает (защита от случайного "
            "индексирования чужих файлов). Пустая строка = ingest выключен."
        ),
    )
    ingest_collection: str = Field(
        default="knowledge_base",
        min_length=1,
        max_length=255,
        description=(
            "Имя коллекции (значение колонки `collection` в `kb_chunks`), "
            "в которую индексируется `ingest_folder`."
        ),
    )
    ingest_collection_description: str = Field(
        default="",
        description=(
            "Description коллекции (видно в kb_list_collections). "
            "Прописывается при первом ensure_collection."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Под discover-flow плагин загружается только если включён —
        значит при load-time `dsn` должен быть валидно заполнен.
        """
        if not self.dsn:
            msg = "postgres.dsn обязателен"
            raise ValueError(msg)
        return self
