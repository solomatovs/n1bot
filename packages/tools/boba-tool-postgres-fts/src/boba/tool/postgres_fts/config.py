"""Конфиг плагина postgres_fts.

Секция `[tool.postgres_fts]`. Используется tools (через FromConfig)
и provider'ами PostgresPool/PgFtsKnowledgeBase.

Плагин-уровневое включение/allowlist (`enable`, `tools`) — забота
framework'а; конфиг плагина их не объявляет (`extra="ignore"`).
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.postgres_fts.models import IndexSpec

__all__ = ["PostgresFtsPluginConfig"]


class PostgresFtsPluginConfig(BobaFlatSettings):
    """PG FTS read-tools: fts_search + fts_list_indexes.

    `dsn` — libpq-строка; для read-only обязательно добавьте параметры
    `default_transaction_read_only=on&statement_timeout=<ms>` прямо в DSN
    (см. boba-db-postgres). Whitelist индексов задаётся через `indexes` —
    LLM видит только то, что описано здесь.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.postgres_fts",
    )

    dsn: str = Field(
        default="",
        description=(
            "libpq DSN; read-only/statement_timeout задаются параметрами "
            "в самом DSN. Обязателен — без него Pool не создать."
        ),
    )
    indexes: list[IndexSpec] = Field(default_factory=list)
    min_pool_size: int = Field(default=1, ge=1)
    max_pool_size: int = Field(default=4, ge=1)
    connect_timeout_sec: float = Field(default=10.0)
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции ts_headline: MaxFragments,MaxWords,MinWords,StartSel,StopSel,..."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k для fts_search.",
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Проверка консистентности — срабатывает при загрузке конфига.

        Под discover-flow плагин загружается только если оператор явно
        включил его (`[tool.postgres_fts] enable=true`). Значит при
        load-time `dsn` + `indexes` должны быть валидно заполнены.
        """
        if not self.dsn:
            msg = "postgres_fts.dsn обязателен"
            raise ValueError(msg)
        if not self.indexes:
            msg = "postgres_fts.indexes должен содержать хотя бы один IndexSpec"
            raise ValueError(msg)
        return self
