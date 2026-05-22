"""`FtsConfig` — конфиг секции `[tool.kb.fts]`.

Read-only FTS-поиск по одной чужой таблице оператора (НЕ по `kb_chunks`).
Оператор декларирует ОДНУ таблицу через `IndexSpec` — LLM не выбирает,
куда ходить (защита от случайных запросов в произвольные таблицы).

DSN — опционален: пустой = используется `[tool.kb.postgres]`-pool
(`PgFtsKnowledgeBase` получает kb-pool через DI). Если указан явно —
создаётся отдельный pool через `PostgresPool.get(...)`.
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.fts.models import IndexSpec

__all__ = ["FtsConfig"]


class FtsConfig(BobaFlatSettings):
    """FTS-поиск по одной whitelist-таблице оператора.

    Config-секция: `[tool.kb.fts]` (sub-section к `[tool.kb]`).
    Загружается, только если `fts_search` присутствует в `[tool.kb].tools`
    allowlist (Framework лениво грузит FromConfig-типы).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.fts",
    )

    dsn: str = Field(
        default="",
        description=(
            "libpq DSN внешней БД. Пусто = fallback на `[tool.kb.postgres]`-"
            "pool (тот же singleton, что и у kb_search). Если задан, должен "
            "содержать `default_transaction_read_only=on&statement_timeout=…` "
            "(read-only гарантия — на уровне DSN, см. boba-db-postgres)."
        ),
    )
    index: IndexSpec = Field(
        description=(
            "Whitelist одной FTS-таблицы: `IndexSpec` с полями "
            "`name/description/schema/table/id_column/tsv_column/"
            "snippet_column/language/metadata_columns`. LLM не выбирает "
            "таблицу — она задана оператором здесь."
        ),
    )
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции `ts_headline`: MaxFragments,MaxWords,MinWords,"
            "StartSel,StopSel,..."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k для fts_search.",
    )
