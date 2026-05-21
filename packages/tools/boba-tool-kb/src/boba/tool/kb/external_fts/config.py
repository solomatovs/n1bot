"""`ExternalFtsConfig` — конфиг секции `[tool.kb.external_fts]`.

Read-only FTS-поиск по чужим таблицам оператора (не по `kb_chunks`):
оператор декларирует whitelist индексов `IndexSpec` — LLM видит только
их через `fts_list_indexes` и ищет через `fts_search`.

Отделён от `KbPluginConfig` (`[tool.kb]`, DSN+embedder+ingest+RRF) и от
`ConfluencePluginConfig`/`ConfluenceIngestConfig` (Confluence pipeline).
Три независимых конфига для трёх независимых каналов:

- `[tool.kb]`                    → наша KB (kb_search, hybrid RRF)
- `[tool.kb.confluence*]`        → Confluence (online + ingest)
- `[tool.kb.external_fts]`       → внешние FTS-таблицы оператора (read-only)

DSN-fallback на `[tool.kb].dsn`: если `dsn` здесь пуст, используется
DSN основной KB-секции, и `PostgresPool.get(...)` отдаёт тот же
process-singleton (DSN + pool_sizes кэшируется ключом). Если внешние
индексы в другой БД — задайте `dsn` явно, тогда создастся отдельный pool.

Pool-sizes/connect_timeout наследуются от `[tool.kb]` всегда — отдельных
полей здесь нет, чтобы исключить случайный mismatch и непреднамеренную
дубликацию пулов на одном DSN.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.external_fts.models import IndexSpec

__all__ = ["ExternalFtsConfig"]


class ExternalFtsConfig(BobaFlatSettings):
    """Whitelist FTS-индексов поверх PostgreSQL: read-only по чужим таблицам.

    Sub-section к `[tool.kb]`. Загружается, только если `fts_search`/
    `fts_list_indexes` присутствуют в `[tool.kb].tools` allowlist
    (Framework лениво грузит FromConfig-типы).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.external_fts",
    )

    dsn: str = Field(
        default="",
        description=(
            "libpq DSN внешней БД. Пустой = fallback на `[tool.kb].dsn` "
            "(одни и те же таблицы что и у kb_search — оператор просто "
            "выставляет свои tsvector-индексы поверх). Если задан, должен "
            "содержать `default_transaction_read_only=on&statement_timeout=…` "
            "(read-only гарантия — на уровне DSN, см. boba-db-postgres)."
        ),
    )
    indexes: list[IndexSpec] = Field(
        default_factory=list,
        description=(
            "Whitelist FTS-индексов: каждый IndexSpec описывает "
            "(name/description/schema/table/id_column/tsv_column/"
            "snippet_column/language/metadata_columns). LLM видит только "
            "то, что описано здесь — auto-discovery по information_schema "
            "намеренно не делается."
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

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Cross-field: `indexes` не пуст, если этот конфиг вообще загружается.

        Под discover-flow конфиг грузится, только если `fts_search`/
        `fts_list_indexes` в allowlist'е. Значит при load-time как минимум
        один `IndexSpec` обязан быть.
        """
        if not self.indexes:
            msg = (
                "kb.external_fts.indexes должен содержать хотя бы один IndexSpec"
            )
            raise ValueError(msg)
        return self
