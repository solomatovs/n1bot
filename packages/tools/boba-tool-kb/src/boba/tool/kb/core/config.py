"""`KbConfig` — конфиг секции `[tool.kb]` (домен KB, без подключений).

Только параметры search/chunker и pinned-коллекция/папка. Подключения
вынесены в отдельные секции:

- `[tool.kb.postgres]`     → `PostgresConnectionConfig` (структурированно)
- `[tool.kb.confluence]`   → `ConfluenceConnectionConfig` (base_url + auth)
- `[tool.kb.embedding]`    → `EmbeddingConfig` (model + base_url + api_key)

Плагин-уровневое включение/allowlist (`enable`, `tools`) — забота
framework'а (`AgentBuilder.discover_plugins`); конфиг плагина их не
объявляет (`extra="ignore"`).
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["KbConfig"]


class KbConfig(BobaFlatSettings):
    """Domain-config KB-плагина: search params, chunker, pinned collection/folder."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb",
    )

    # pinned collection (read+write target)
    collection: str = Field(
        default="knowledge_base",
        min_length=1,
        max_length=255,
        description=(
            "Имя коллекции (значение колонки `collection` в `kb_chunks`), "
            "в которую пишут все ingest-tools (`files_ingest`, "
            "`confluence_space_ingest`, `confluence_page_ingest`) и из "
            "которой читает `kb_search`. Один pinned-target — оператор "
            "не даёт LLM создавать произвольные коллекции."
        ),
    )
    collection_description: str = Field(
        default="",
        description=(
            "Description коллекции; прописывается при первом "
            "`ensure_collection` (видно в служебных запросах)."
        ),
    )

    # files_ingest (FS-источник)
    files_folder: str = Field(
        default="./local/docs",
        description=(
            "Папка с файлами для `files_ingest` (`.md`/`.html`/`.htm`). "
            "Оператор закрепляет выбор папки за собой — LLM не выбирает "
            "(защита от индексирования чужих файлов). Дефолт `./local/docs`."
        ),
    )

    # FTS / search
    fts_language: str = Field(
        default="russian",
        description=(
            "PostgreSQL text search configuration для tsvector колонки. "
            "Должен быть установленным `pg_ts_config` именем (`russian`, "
            "`english`, `simple`, ...). Меняется только пересозданием "
            "kb_chunks (см. `migrations/001_init.sql`)."
        ),
    )
    snippet_chars: int = Field(
        default=300,
        ge=1,
        description="Максимальная длина сниппета документа в `kb_search`.",
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

    # embedder — вынесен в [tool.kb.embedding] (`EmbeddingConfig`)

    # chunker
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
            "`OverlapCharSplitter.chunk_overlap`). 0 = без перекрытия."
        ),
    )
